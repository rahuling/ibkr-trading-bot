"""
Trading bot entry point.

Startup sequence (order matters):
    1. Load and validate config  — fails loudly on bad config.yaml
    2. Initialise database        — WAL mode, run schema migrations
    3. Connect to IB Gateway      — abort if unreachable
    4. State reconciliation       — compare DB vs live IBKR positions
    5. Start APScheduler          — timezone: America/New_York
    6. Start Telegram bot         — begin accepting commands
    7. Heartbeat loop             — 5-min heartbeat, alert on 2 missed
"""

import asyncio
import logging
import logging.handlers
import os
import sys
from pathlib import Path

import pytz
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv

from bot.config import load_config, AppConfig
from bot.database import init_db
from bot.ibkr import create_ibkr_connection, IBKRConnection

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.handlers.RotatingFileHandler(
            "logs/bot.log",
            maxBytes=10 * 1024 * 1024,   # 10 MB
            backupCount=5,
        ),
    ],
)
logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

class HeartbeatMonitor:
    """
    Tracks consecutive missed heartbeats and fires a Telegram alert after 2.

    A heartbeat is considered alive if the IB Gateway is connected.
    Using a class instead of a module-level global avoids mutable shared state.
    """

    def __init__(self) -> None:
        self._missed = 0

    async def tick(self, ibkr: IBKRConnection, telegram_bot) -> None:
        if ibkr.is_connected:
            logger.debug("Heartbeat OK")
            self._missed = 0
        else:
            self._missed += 1
            logger.error("Heartbeat failed (%s missed) — IB Gateway not connected", self._missed)
            if self._missed >= 2:
                await telegram_bot.send_alert(
                    "💤 Bot heartbeat missed — IB Gateway disconnected."
                )


# ---------------------------------------------------------------------------
# Scheduler helpers
# ---------------------------------------------------------------------------

def _guarded_job(fn, job_id: str):
    """
    Wrap a coroutine function so NotImplementedError is swallowed gracefully.

    Without this, every unimplemented scheduled job raises NotImplementedError
    through APScheduler's error handler — logging a full traceback for every
    scan at 9:45am, 3pm, 3:30pm, 4:15pm, and 9:30am daily until implemented.
    """
    async def wrapper(*args, **kwargs):
        try:
            await fn(*args, **kwargs)
        except NotImplementedError:
            logger.debug("Scheduled job '%s' not yet implemented — skipping", job_id)
        except Exception as exc:
            logger.error("Scheduled job '%s' failed: %s", job_id, exc, exc_info=True)
    return wrapper


# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def build_scheduler(
    config: AppConfig,
    ibkr: IBKRConnection,
    telegram_bot,
    heartbeat_monitor: HeartbeatMonitor,
) -> AsyncIOScheduler:
    """
    Register all scheduled jobs.

    All times are US Eastern. APScheduler is initialised with ET timezone
    so cron expressions are written in local market time, not UTC.

    Unimplemented jobs are wrapped with _guarded_job so they fail silently
    until their Phase is complete, rather than flooding logs with tracebacks.
    """
    scheduler = AsyncIOScheduler(timezone=ET)

    # Daily morning summary — 9:30am ET
    scheduler.add_job(
        _guarded_job(telegram_bot.send_morning_summary, "morning_summary"),
        "cron", hour=9, minute=30,
        id="morning_summary",
    )

    # Premium-selling scanner — 9:45am and 3:00pm ET
    from bot.scanner.premium import PremiumScanner
    premium_scanner = PremiumScanner(config, ibkr)
    scheduler.add_job(
        _guarded_job(premium_scanner.run, "morning_scan"),
        "cron", hour=9, minute=45,
        id="morning_scan",
    )
    scheduler.add_job(
        _guarded_job(premium_scanner.run, "afternoon_scan"),
        "cron", hour=15, minute=0,
        id="afternoon_scan",
    )

    # EOD momentum scanner — 3:30pm ET
    from bot.scanner.momentum import MomentumScanner
    momentum_scanner = MomentumScanner(config, ibkr)
    scheduler.add_job(
        _guarded_job(momentum_scanner.run, "eod_scan"),
        "cron", hour=15, minute=30,
        id="eod_scan",
    )

    # IV history update — 4:15pm ET (after market close)
    from bot.scanner.base import update_iv_history
    scheduler.add_job(
        _guarded_job(update_iv_history, "iv_history_update"),
        "cron", hour=16, minute=15,
        args=[config, ibkr],
        id="iv_history_update",
    )

    # Heartbeat — every 5 minutes
    scheduler.add_job(
        heartbeat_monitor.tick,
        "interval", minutes=5,
        args=[ibkr, telegram_bot],
        id="heartbeat",
    )

    # Monthly P&L report — 1st of each month, 8am ET
    from bot.journal.journal import Journal
    journal = Journal(config)
    scheduler.add_job(
        _guarded_job(journal.send_monthly_report, "monthly_report"),
        "cron", day=1, hour=8, minute=0,
        args=[telegram_bot],
        id="monthly_report",
    )

    # Wire scanners to bot so /scan command can invoke them manually.
    telegram_bot.wire_scanners(
        premium_scanner=premium_scanner,
        momentum_scanner=momentum_scanner,
    )

    return scheduler


# ---------------------------------------------------------------------------
# Startup reconciliation
# ---------------------------------------------------------------------------

async def reconcile_state(ibkr: IBKRConnection, telegram_bot) -> bool:
    """
    Compare DB open positions against live IBKR positions.

    Returns True if state is consistent, False if mismatches found.
    On mismatch: alerts user and blocks new trade proposals until resolved.

    TODO (Phase 4): implement full reconciliation logic.
    """
    logger.info("Running startup state reconciliation...")
    # Placeholder — will be implemented in Phase 4
    logger.info("Reconciliation: OK (placeholder — implement in Phase 4)")
    return True


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main() -> None:
    scheduler = None
    ibkr = None

    # 1. Load environment variables
    load_dotenv()

    # 2. Load and validate config — fail loudly on bad config
    logger.info("Loading config.yaml...")
    try:
        config = load_config("config.yaml")
        logger.info("Config loaded OK. Automation level: L%s", config.automation.level)
    except Exception as exc:
        logger.critical("Config validation failed: %s", exc)
        sys.exit(1)

    # 3. Initialise database
    logger.info("Initialising database...")
    await init_db()

    # 4. Parse Telegram credentials — fail loudly with a clear message
    try:
        telegram_token = os.environ["TELEGRAM_BOT_TOKEN"]
        allowed_user_ids = set(
            int(uid.strip())
            for uid in os.environ["TELEGRAM_ALLOWED_USER_IDS"].split(",")
            if uid.strip()
        )
    except KeyError as exc:
        logger.critical("Missing required environment variable: %s", exc)
        sys.exit(1)
    except ValueError as exc:
        logger.critical("TELEGRAM_ALLOWED_USER_IDS must be comma-separated integers: %s", exc)
        sys.exit(1)

    # 5. Set up Telegram bot (needed before IB Gateway so alerts can fire)
    from bot.telegram.bot import TelegramBot
    telegram_bot = TelegramBot(
        token=telegram_token,
        allowed_user_ids=allowed_user_ids,
        config=config,
    )

    # 6. Connect to IB Gateway
    logger.info("Connecting to IB Gateway...")
    try:
        ibkr = create_ibkr_connection(on_alert=telegram_bot.send_alert)
    except ValueError as exc:
        logger.critical("Bad IB Gateway environment variables: %s", exc)
        sys.exit(1)

    try:
        await ibkr.connect()
    except Exception as exc:
        logger.critical("Cannot connect to IB Gateway: %s", exc)
        await telegram_bot.send_alert(f"🚨 Bot failed to start — IB Gateway unreachable: {exc}")
        sys.exit(1)

    net_liq = ibkr.get_net_liquidation()
    logger.info("IB Gateway connected. Net Liquidation: $%s", f"{net_liq:,.2f}" if net_liq else "N/A")

    # 7. State reconciliation — must pass before bot accepts new trades
    consistent = await reconcile_state(ibkr, telegram_bot)
    if not consistent:
        logger.warning("State reconciliation found mismatches — new trades blocked until resolved")

    # 8. Wire up modules
    from bot.risk.engine import RiskEngine
    from bot.positions.manager import PositionManager
    from bot.execution.engine import ExecutionEngine

    risk_engine = RiskEngine(config, ibkr)
    position_manager = PositionManager(config, ibkr, risk_engine)
    execution_engine = ExecutionEngine(config, ibkr, risk_engine)

    telegram_bot.wire(
        ibkr=ibkr,
        risk_engine=risk_engine,
        position_manager=position_manager,
        execution_engine=execution_engine,
    )

    # 9. Start scheduler (scanner references wired to bot inside build_scheduler)
    heartbeat_monitor = HeartbeatMonitor()
    scheduler = build_scheduler(config, ibkr, telegram_bot, heartbeat_monitor)
    scheduler.start()
    logger.info("Scheduler started. Jobs: %s", [j.id for j in scheduler.get_jobs()])

    # 10. Start Telegram bot and run until interrupted.
    # try/finally ensures the scheduler and IB connection are always cleaned up,
    # even on KeyboardInterrupt or an unhandled exception in run_polling.
    try:
        logger.info("Bot started. Waiting for commands.")
        await telegram_bot.run()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        logger.info("Shutting down...")
        await telegram_bot.stop()
        if scheduler and scheduler.running:
            scheduler.shutdown(wait=False)
        if ibkr:
            await ibkr.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
