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

_missed_heartbeats = 0

async def heartbeat(telegram_bot) -> None:
    """Log a heartbeat every 5 minutes. Alert on 2 consecutive misses."""
    global _missed_heartbeats
    try:
        logger.debug("Heartbeat OK")
        _missed_heartbeats = 0
    except Exception as exc:
        _missed_heartbeats += 1
        logger.error("Heartbeat failed (%s missed): %s", _missed_heartbeats, exc)
        if _missed_heartbeats >= 2:
            await telegram_bot.send_alert("💤 Bot heartbeat missed — possible issue.")

# ---------------------------------------------------------------------------
# Scheduler setup
# ---------------------------------------------------------------------------

def build_scheduler(config: AppConfig, ibkr: IBKRConnection, telegram_bot) -> AsyncIOScheduler:
    """
    Register all scheduled jobs.

    All times are US Eastern. APScheduler is initialised with ET timezone
    so cron expressions are written in local market time, not UTC.
    """
    scheduler = AsyncIOScheduler(timezone=ET)

    # Daily morning summary — 9:30am ET
    scheduler.add_job(
        telegram_bot.send_morning_summary,
        "cron", hour=9, minute=30,
        id="morning_summary",
    )

    # Premium-selling scanner — 9:45am and 3:00pm ET
    from bot.scanner.premium import PremiumScanner
    premium_scanner = PremiumScanner(config, ibkr)
    scheduler.add_job(
        premium_scanner.run,
        "cron", hour=9, minute=45,
        id="morning_scan",
    )
    scheduler.add_job(
        premium_scanner.run,
        "cron", hour=15, minute=0,
        id="afternoon_scan",
    )

    # EOD momentum scanner — 3:30pm ET
    from bot.scanner.momentum import MomentumScanner
    momentum_scanner = MomentumScanner(config, ibkr)
    scheduler.add_job(
        momentum_scanner.run,
        "cron", hour=15, minute=30,
        id="eod_scan",
    )

    # IV history update — 4:15pm ET (after market close)
    from bot.scanner.base import update_iv_history
    scheduler.add_job(
        update_iv_history,
        "cron", hour=16, minute=15,
        args=[config, ibkr],
        id="iv_history_update",
    )

    # Heartbeat — every 5 minutes
    scheduler.add_job(
        heartbeat,
        "interval", minutes=5,
        args=[telegram_bot],
        id="heartbeat",
    )

    # Monthly P&L report — 1st of each month, 8am ET
    from bot.journal.journal import Journal
    journal = Journal(config)
    scheduler.add_job(
        journal.send_monthly_report,
        "cron", day=1, hour=8, minute=0,
        args=[telegram_bot],
        id="monthly_report",
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

    # 4. Set up Telegram bot (needed before IB Gateway so alerts can fire)
    from bot.telegram.bot import TelegramBot
    telegram_bot = TelegramBot(
        token=os.environ["TELEGRAM_BOT_TOKEN"],
        allowed_user_ids=set(
            int(uid.strip())
            for uid in os.environ["TELEGRAM_ALLOWED_USER_IDS"].split(",")
        ),
        config=config,
    )

    # 5. Connect to IB Gateway
    logger.info("Connecting to IB Gateway...")
    ibkr = create_ibkr_connection(on_alert=telegram_bot.send_alert)
    try:
        await ibkr.connect()
    except Exception as exc:
        logger.critical("Cannot connect to IB Gateway: %s", exc)
        await telegram_bot.send_alert(f"🚨 Bot failed to start — IB Gateway unreachable: {exc}")
        sys.exit(1)

    net_liq = ibkr.get_net_liquidation()
    logger.info("IB Gateway connected. Net Liquidation: $%s", f"{net_liq:,.2f}" if net_liq else "N/A")

    # 6. State reconciliation — must pass before bot accepts new trades
    consistent = await reconcile_state(ibkr, telegram_bot)
    if not consistent:
        logger.warning("State reconciliation found mismatches — new trades blocked until resolved")

    # 7. Wire up modules
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

    # 8. Start scheduler
    scheduler = build_scheduler(config, ibkr, telegram_bot)
    scheduler.start()
    logger.info("Scheduler started. Jobs: %s", [j.id for j in scheduler.get_jobs()])

    # 9. Start Telegram bot (runs until interrupted)
    logger.info("Bot started. Waiting for commands.")
    await telegram_bot.run()


if __name__ == "__main__":
    import logging.handlers
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
