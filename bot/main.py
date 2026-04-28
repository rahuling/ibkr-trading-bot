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
    Alerts only on the transition into and out of the disconnected state,
    not on every tick — prevents flooding while the gateway stays down.
    """

    def __init__(self) -> None:
        self._missed = 0
        self._alerted = False  # True while in a disconnected alert state

    async def tick(self, ibkr: IBKRConnection, telegram_bot) -> None:
        if ibkr.is_connected:
            if self._alerted:
                await telegram_bot.send_alert("✅ IB Gateway reconnected — bot resuming normally.")
                self._alerted = False
            self._missed = 0
            logger.debug("Heartbeat OK")
        else:
            self._missed += 1
            logger.error("Heartbeat failed (%s missed) — IB Gateway not connected", self._missed)
            if self._missed >= 2 and not self._alerted:
                self._alerted = True
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
    position_manager=None,
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

    # Position monitor fallback — every 5 minutes
    # Updates P&L / Greeks in positions table and fires management alerts.
    if position_manager:
        scheduler.add_job(
            _guarded_job(position_manager.check_all_positions, "position_monitor"),
            "interval", minutes=5,
            id="position_monitor",
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

async def reconcile_state(
    ibkr: IBKRConnection,
    telegram_bot,
    execution_engine=None,
) -> bool:
    """
    Compare DB open positions against live IBKR positions.

    Returns True if state is consistent, False if mismatches found.
    On mismatch: alerts user but does NOT block — the user should resolve
    via /reconcile or manual TWS action.
    """
    from bot.database import get_db

    logger.info("Running startup state reconciliation...")

    # 1. Recover orphaned orders (crash-mid-order recovery)
    if execution_engine:
        async with get_db() as db:
            await execution_engine.recover_orphaned_orders(db)

    # 2. Compare DB open trades vs IBKR live option positions
    async with get_db() as db:
        async with db.execute(
            "SELECT underlying FROM trades WHERE status = 'open'"
        ) as cur:
            rows = await cur.fetchall()
    db_underlyings = {row["underlying"] for row in rows}

    try:
        ibkr_positions = ibkr.ib.positions()
        ibkr_underlyings = {
            p.contract.symbol
            for p in ibkr_positions
            if p.contract.secType in ("OPT", "BAG")
        }
    except Exception as exc:
        logger.error("IBKR position query during reconcile failed: %s", exc)
        return True  # Don't block on IBKR query failure

    mismatches = (db_underlyings - ibkr_underlyings) | (ibkr_underlyings - db_underlyings)
    if mismatches:
        msg = (
            f"⚠️ Reconciliation mismatch on startup: {', '.join(sorted(mismatches))}\n"
            "Use /reconcile to investigate."
        )
        logger.warning(msg)
        await telegram_bot.send_alert(msg)
        return False

    logger.info("Reconciliation: OK — DB and IBKR positions match")
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

    # 7. Wire up modules (before reconciliation so execution_engine is available)
    from bot.risk.engine import RiskEngine
    from bot.positions.manager import PositionManager
    from bot.execution.engine import ExecutionEngine

    risk_engine = RiskEngine(config, ibkr)
    position_manager = PositionManager(config, ibkr, risk_engine)
    execution_engine = ExecutionEngine(config, ibkr, risk_engine)

    execution_engine.set_position_manager(position_manager)

    telegram_bot.wire(
        ibkr=ibkr,
        risk_engine=risk_engine,
        position_manager=position_manager,
        execution_engine=execution_engine,
    )

    # 8. State reconciliation (orphan recovery + DB vs IBKR position check)
    consistent = await reconcile_state(ibkr, telegram_bot, execution_engine)
    if not consistent:
        logger.warning("State reconciliation found mismatches — check /reconcile")

    # 8b. Subscribe to real-time price ticks and wire assignment detection
    await position_manager.subscribe_all_open_positions()
    position_manager.setup_assignment_detection()

    # 9. Start scheduler (scanner references wired to bot inside build_scheduler)
    heartbeat_monitor = HeartbeatMonitor()
    scheduler = build_scheduler(config, ibkr, telegram_bot, heartbeat_monitor, position_manager)
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
