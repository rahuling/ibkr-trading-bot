"""
M3: Telegram command handlers.

All /command implementations. Registered with the Application in bot.py.

PRD reference: §5 M3 Telegram Interface — Commands table.
"""

import logging
import os
import time as time_module
from collections import defaultdict

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, Application

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------

_last_call: dict = defaultdict(float)

# Commands that trigger expensive operations (IBKR API calls, full scans).
# Keyed by command name → minimum seconds between invocations per user.
_RATE_LIMITS = {
    "scan":       30,
    "reconcile":  60,
    "positions":  10,
    "risk":       10,
}


def _check_rate_limit(user_id: int, command: str) -> bool:
    """
    Return True if the command is allowed, False if the user is calling
    too fast.

    Prevents a single allowlisted user from spamming expensive operations.
    Uses monotonic clock so it is immune to system clock changes.
    """
    cooldown = _RATE_LIMITS.get(command, 0)
    if cooldown == 0:
        return True
    key = (user_id, command)
    now = time_module.monotonic()
    if now - _last_call[key] < cooldown:
        return False
    _last_call[key] = now
    return True


# ---------------------------------------------------------------------------
# Setconfig allowlist
# ---------------------------------------------------------------------------

# Only these dot-path config params may be mutated at runtime via /setconfig.
# All other params require a config.yaml edit + restart.
# When implementing Phase 4: add range validation per param alongside this set.
_SETCONFIG_ALLOWED: frozenset = frozenset({
    "risk.daily_loss_limit_pct",
    "risk.weekly_loss_limit_pct",
    "risk.monthly_loss_limit_pct",
    "risk.min_ivr_core",
    "risk.min_ivr_tactical",
    "risk.earnings_blackout_pre_days",
    "risk.earnings_blackout_post_days",
    "automation.level",
})


# ---------------------------------------------------------------------------
# Command registration
# ---------------------------------------------------------------------------

def register_commands(app: Application, bot, auth_filter) -> None:
    """Register all command handlers with the Telegram application."""
    handlers = [
        ("start",         cmd_start),
        ("help",          cmd_help),
        ("status",        cmd_status),
        ("scan",          cmd_scan),
        ("positions",     cmd_positions),
        ("approve",       cmd_approve),
        ("reject",        cmd_reject),
        ("close",         cmd_close),
        ("roll",          cmd_roll),
        ("risk",          cmd_risk),
        ("pause",         cmd_pause),
        ("resume",        cmd_resume),
        ("journal",       cmd_journal),
        ("wheel",         cmd_wheel),
        ("analyze",       cmd_analyze),
        ("setconfig",     cmd_setconfig),
        ("config",        cmd_config),
        ("watchlist",     cmd_watchlist),
        ("addticker",     cmd_addticker),
        ("removeticker",  cmd_removeticker),
        ("reconcile",     cmd_reconcile),
    ]
    for command, handler in handlers:
        app.add_handler(
            CommandHandler(command, lambda u, c, h=handler, b=bot: h(u, c, b), auth_filter)
        )


# ---------------------------------------------------------------------------
# Implemented (Phase 1 milestone)
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    await update.message.reply_text("Trading bot online. Use /help for commands.")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    text = (
        "📖 *Available Commands*\n\n"
        "*Scanning & Proposals*\n"
        "/scan — trigger manual scan\n"
        "/approve \\[id\\] — approve a proposal\n"
        "/reject \\[id\\] \\[reason\\] — reject a proposal\n\n"
        "*Positions*\n"
        "/positions — open positions with Greeks\n"
        "/close \\[id\\] — close a position\n"
        "/roll \\[id\\] — initiate roll logic\n"
        "/wheel \\[id\\] — full Wheel cycle P&L\n\n"
        "*Risk & P&L*\n"
        "/risk — capital allocation and limits\n"
        "/journal \\[days\\] — trade journal \\(default 30\\)\n"
        "/analyze \\[tag\\] — win rate by rule condition\n\n"
        "*Configuration*\n"
        "/config — current configuration\n"
        "/setconfig \\[param\\] \\[value\\] — adjust parameter\n"
        "/watchlist — current watchlist\n"
        "/addticker \\[ticker\\] — add to watchlist\n"
        "/removeticker \\[ticker\\] — remove from watchlist\n\n"
        "*System*\n"
        "/status — bot health and IB Gateway connection\n"
        "/pause — halt scanning and execution\n"
        "/resume — resume after pause\n"
        "/reconcile — force state reconciliation\n"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """
    Show bot health: uptime, last scan time, IB Gateway status, account balance.

    This is the Phase 1 milestone command — must work end-to-end before
    any other development proceeds.
    """
    connected = bot.ibkr.is_connected if bot.ibkr else False
    net_liq = bot.ibkr.get_net_liquidation() if bot.ibkr and connected else None
    account = bot.ibkr.get_account_id() if bot.ibkr and connected else None

    gw_status = "🟢 Connected" if connected else "🔴 Disconnected"
    balance = f"${net_liq:,.2f}" if net_liq else "N/A"

    # Use the explicit TRADING_MODE env var rather than guessing from the
    # account ID string — account IDs don't reliably contain "paper".
    trading_mode = os.getenv("TRADING_MODE", "paper").upper()
    mode = "PAPER" if trading_mode == "PAPER" else "LIVE"

    text = (
        f"📊 *Bot Status*\n"
        f"──────────────────────\n"
        f"IB Gateway:  {gw_status}\n"
        f"Account:     {account or 'N/A'}  \\({mode}\\)\n"
        f"Balance:     {balance}\n"
        f"Automation:  L{bot.config.automation.level}\n"
        f"Paused:      {'Yes ⏸' if (bot.risk_engine and bot.risk_engine.is_paused) else 'No'}\n"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


# ---------------------------------------------------------------------------
# Stubs — implemented in later phases
# ---------------------------------------------------------------------------

async def cmd_scan(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Trigger manual scan. TODO (Phase 2)."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "scan"):
        await update.message.reply_text("Please wait before scanning again.")
        return
    await update.message.reply_text("Manual scan triggered. TODO: Phase 2")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show open positions with Greeks. TODO (Phase 4)."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "positions"):
        await update.message.reply_text("Please wait before refreshing positions.")
        return
    await update.message.reply_text("/positions: TODO Phase 4")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Approve and execute a trade proposal. TODO (Phase 3)."""
    await update.message.reply_text("/approve: TODO Phase 3")


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Reject a trade proposal. TODO (Phase 3)."""
    await update.message.reply_text("/reject: TODO Phase 3")


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Manually close a position. TODO (Phase 3)."""
    await update.message.reply_text("/close: TODO Phase 3")


async def cmd_roll(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Initiate roll logic. TODO (Phase 4)."""
    await update.message.reply_text("/roll: TODO Phase 4")


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show risk dashboard. TODO (Phase 4)."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "risk"):
        await update.message.reply_text("Please wait before refreshing risk data.")
        return
    await update.message.reply_text("/risk: TODO Phase 4")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Halt scanning and execution."""
    if bot.risk_engine:
        bot.risk_engine.pause("User command /pause")
        await update.message.reply_text("⏸ Bot paused. Use /resume to restart.")
    else:
        await update.message.reply_text("Risk engine not ready.")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Resume after pause."""
    if bot.risk_engine:
        bot.risk_engine.resume()
        await update.message.reply_text("▶️ Bot resumed.")
    else:
        await update.message.reply_text("Risk engine not ready.")


async def cmd_journal(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show trade journal. TODO (Phase 5)."""
    await update.message.reply_text("/journal: TODO Phase 5")


async def cmd_wheel(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show full Wheel cycle P&L. TODO (Phase 5)."""
    await update.message.reply_text("/wheel: TODO Phase 5")


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show rule performance analytics. TODO (Phase 5)."""
    await update.message.reply_text("/analyze: TODO Phase 5")


async def cmd_setconfig(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """
    Adjust a config parameter at runtime. TODO (Phase 4): persist + apply change.

    Only params in _SETCONFIG_ALLOWED may be mutated. All others require a
    config.yaml edit and restart. This prevents accidental or malicious mutation
    of critical structural parameters (bucket allocations, strategy targets).
    """
    args = context.args if context.args else []
    if len(args) != 2:
        allowed = "\n".join(f"  • {p}" for p in sorted(_SETCONFIG_ALLOWED))
        await update.message.reply_text(
            f"Usage: /setconfig <param> <value>\n\nMutable params:\n{allowed}"
        )
        return

    param, value = args[0], args[1]
    if param not in _SETCONFIG_ALLOWED:
        allowed = "\n".join(f"  • {p}" for p in sorted(_SETCONFIG_ALLOWED))
        await update.message.reply_text(
            f"'{param}' is not a mutable parameter.\n\nAllowed:\n{allowed}"
        )
        return

    # Phase 4: validate value type/range, apply to live config, write audit log.
    await update.message.reply_text(f"/setconfig {param} {value}: TODO Phase 4")


async def cmd_config(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show current configuration."""
    cfg = bot.config
    text = (
        f"*Current Configuration*\n"
        f"Automation: L{cfg.automation.level}\n"
        f"Buckets: Core {cfg.risk.core_bucket_pct*100:.0f}% / "
        f"Tactical {cfg.risk.tactical_bucket_pct*100:.0f}% / "
        f"Momentum {cfg.risk.momentum_bucket_pct*100:.0f}% / "
        f"Reserve {cfg.risk.reserve_pct*100:.0f}%\n"
        f"Daily stop: {cfg.risk.daily_loss_limit_pct*100:.1f}%  "
        f"Weekly: {cfg.risk.weekly_loss_limit_pct*100:.1f}%  "
        f"Monthly: {cfg.risk.monthly_loss_limit_pct*100:.1f}%\n"
        f"IVR min: Core {cfg.risk.min_ivr_core} / Tactical {cfg.risk.min_ivr_tactical}\n"
        f"CSP delta target: {cfg.trading.csp.target_delta}  "
        f"DTE: {cfg.trading.csp.dte_min}–{cfg.trading.csp.dte_max}\n"
        f"Spread width: ${cfg.trading.spread.spread_width}  "
        f"Max loss: ${cfg.risk.max_spread_loss}\n"
        f"LEAP stop: {cfg.leap.stop_loss_pct*100:.0f}%  "
        f"Target: {cfg.leap.profit_target_pct*100:.0f}%\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show current watchlist."""
    core = ", ".join(bot.config.scanner.watchlist.core)
    tactical = ", ".join(bot.config.scanner.watchlist.tactical)
    momentum = ", ".join(bot.config.leap.momentum_watchlist)
    text = f"*Watchlist*\nCore: {core}\nTactical: {tactical}\nMomentum: {momentum}"
    await update.message.reply_text(text, parse_mode="Markdown")


async def cmd_addticker(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Add ticker to watchlist. TODO (Phase 2)."""
    await update.message.reply_text("/addticker: TODO Phase 2")


async def cmd_removeticker(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Remove ticker from watchlist. TODO (Phase 2)."""
    await update.message.reply_text("/removeticker: TODO Phase 2")


async def cmd_reconcile(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Force state reconciliation. TODO (Phase 4)."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "reconcile"):
        await update.message.reply_text("Reconciliation already in progress — please wait.")
        return
    await update.message.reply_text("/reconcile: TODO Phase 4")
