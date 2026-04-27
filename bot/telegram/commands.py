"""
M3: Telegram command handlers.

All /command implementations. Registered with the Application in bot.py.

PRD reference: §5 M3 Telegram Interface — Commands table.
"""

import json
import logging
import os
import re
import time as time_module
from collections import defaultdict
from datetime import datetime, timezone

from telegram import Update
from telegram.ext import CommandHandler, ContextTypes, Application

from bot.database import get_db

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MarkdownV2 helpers
# ---------------------------------------------------------------------------

def _e(value) -> str:
    """Escape a value for use in a MarkdownV2 message body."""
    return re.sub(r'([_*\[\]()~`>#+=|{}.!\-])', r'\\\1', str(value))


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
# Contract reconstruction helper (used by /approve)
# ---------------------------------------------------------------------------

async def _build_contract_for_approval(bot, strategy: str, proposal_data: dict):
    """
    Reconstruct an IBKR contract and determine the opening limit price
    from a stored proposal JSON.

    CSP: re-qualifies the option (verifies it is still listed).
    Spread: builds a BAG using stored con_ids (permanent IBKR identifiers).
    Returns (contract, price).
    """
    if strategy == "CSP":
        from ib_async import Option
        expiry_str = proposal_data["expiry"].replace("-", "")  # "20260516"
        contract = Option(
            proposal_data["underlying"], expiry_str, proposal_data["strike"], "P", "SMART"
        )
        [qualified] = await bot.ibkr.ib.qualifyContractsAsync(contract)
        return qualified, proposal_data["credit_per_share"]

    if strategy == "BullPutSpread":
        from bot.builder.spread import build_bag_contract
        contract = build_bag_contract(
            bot.ibkr,
            proposal_data["underlying"],
            proposal_data["short_put_con_id"],
            proposal_data["long_put_con_id"],
        )
        return contract, proposal_data["credit_per_share"]

    raise ValueError(f"Unknown strategy: {strategy}")


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
    net_liq_currency = bot.ibkr.get_net_liquidation_currency() if bot.ibkr and connected else None
    account = bot.ibkr.get_account_id() if bot.ibkr and connected else None

    gw_status = "🟢 Connected" if connected else "🔴 Disconnected"
    if net_liq is not None:
        symbol = "$" if net_liq_currency in ("USD", None) else ""
        suffix = f" {net_liq_currency}" if net_liq_currency and net_liq_currency != "USD" else ""
        balance = _e(f"{symbol}{net_liq:,.2f}{suffix}")
    else:
        balance = "N/A"

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
    """Trigger manual premium-selling scan."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "scan"):
        await update.message.reply_text("Please wait before scanning again.")
        return

    if not bot.premium_scanner:
        await update.message.reply_text("Scanner not initialized.")
        return

    await update.message.reply_text("🔍 Running premium-selling scan...")
    try:
        candidates = await bot.premium_scanner.run()
        if not candidates:
            await update.message.reply_text(
                "No candidates found. IV may be low across watchlist, or all tickers are"
                " in earnings blackout / already have open positions."
            )
        else:
            await update.message.reply_text(
                f"Scan complete — {len(candidates)} proposal(s) sent above."
            )
    except Exception as exc:
        logger.error("cmd_scan failed: %s", exc, exc_info=True)
        await update.message.reply_text("Scan failed — check logs.")


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show open positions with Greeks. TODO (Phase 4)."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "positions"):
        await update.message.reply_text("Please wait before refreshing positions.")
        return
    await update.message.reply_text("/positions: TODO Phase 4")


async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Approve a pending trade proposal and submit the order to IBKR."""
    args = context.args if context.args else []
    if not args:
        await update.message.reply_text("Usage: /approve <proposal_id>")
        return

    proposal_id = args[0].upper()

    if not bot.execution_engine:
        await update.message.reply_text("Execution engine not ready.")
        return

    if bot.risk_engine and bot.risk_engine.is_paused:
        await update.message.reply_text("Bot is paused — use /resume before approving trades.")
        return

    if bot.execution_engine.is_in_blackout():
        await update.message.reply_text(
            "❌ Order blocked — market open/close blackout window active.\n"
            "Try again after the blackout clears."
        )
        return

    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM proposals WHERE proposal_id = ?", (proposal_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await update.message.reply_text(f"Proposal {proposal_id} not found.")
            return

        proposal = dict(row)

        if proposal["status"] != "pending":
            await update.message.reply_text(
                f"Proposal {proposal_id} is already {proposal['status']} — cannot approve."
            )
            return

        expires_at = datetime.fromisoformat(proposal["expires_at"])
        if datetime.now(timezone.utc) > expires_at:
            await db.execute(
                "UPDATE proposals SET status = 'expired' WHERE proposal_id = ?", (proposal_id,)
            )
            await db.commit()
            await update.message.reply_text(
                f"Proposal {proposal_id} has expired. Run /scan for fresh proposals."
            )
            return

        strategy = proposal["strategy"]
        underlying = proposal["underlying"]
        proposal_data = json.loads(proposal["trade_card_json"])

        try:
            contract, price = await _build_contract_for_approval(bot, strategy, proposal_data)
        except Exception as exc:
            logger.error("Contract reconstruction failed for %s: %s", proposal_id, exc)
            await update.message.reply_text(f"❌ Failed to reconstruct contract: {exc}")
            return

        await update.message.reply_text(
            f"⏳ Submitting {underlying} {strategy} at ${price:.2f}..."
        )

        try:
            client_order_id = await bot.execution_engine.submit_order(
                db, contract, price, quantity=1, proposal_id=proposal_id
            )
        except RuntimeError as exc:
            await update.message.reply_text(f"❌ {exc}")
            return
        except Exception as exc:
            logger.error("submit_order failed for %s: %s", proposal_id, exc, exc_info=True)
            await update.message.reply_text("Order submission failed — check logs.")
            return

        await db.execute(
            "UPDATE proposals SET status = 'approved', actioned_at = ? WHERE proposal_id = ?",
            (datetime.now(timezone.utc).isoformat(), proposal_id),
        )
        await db.commit()

    await update.message.reply_text(
        f"✅ Order submitted: {underlying} {strategy}\n"
        f"Ref: {client_order_id[:8]}...  |  Limit: ${price:.2f}\n"
        f"You'll be notified on fill."
    )


async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Reject a pending trade proposal."""
    args = context.args if context.args else []
    if not args:
        await update.message.reply_text("Usage: /reject <proposal_id> [reason]")
        return

    proposal_id = args[0].upper()
    reason = " ".join(args[1:]) if len(args) > 1 else "manual"

    async with get_db() as db:
        async with db.execute(
            "SELECT status, underlying, strategy FROM proposals WHERE proposal_id = ?",
            (proposal_id,),
        ) as cur:
            row = await cur.fetchone()

        if not row:
            await update.message.reply_text(f"Proposal {proposal_id} not found.")
            return

        if row["status"] != "pending":
            await update.message.reply_text(
                f"Proposal {proposal_id} is already {row['status']}."
            )
            return

        await db.execute(
            "UPDATE proposals SET status = 'rejected', actioned_at = ? WHERE proposal_id = ?",
            (datetime.now(timezone.utc).isoformat(), proposal_id),
        )
        await db.commit()

    await update.message.reply_text(
        f"❌ Rejected {proposal_id} ({row['underlying']} {row['strategy']}) — {reason}"
    )


async def cmd_close(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Manually close an open position by trade ID."""
    args = context.args if context.args else []
    if not args:
        await update.message.reply_text(
            "Usage: /close <trade_id>\n\nUse /positions to see trade IDs."
        )
        return

    trade_id = args[0]
    order_type = args[1] if len(args) > 1 else "limit"
    if order_type not in ("limit", "bid", "market"):
        await update.message.reply_text("order_type must be: limit, bid, or market")
        return

    if not bot.execution_engine:
        await update.message.reply_text("Execution engine not ready.")
        return

    async with get_db() as db:
        async with db.execute(
            "SELECT * FROM trades WHERE trade_id = ? AND status = 'open'", (trade_id,)
        ) as cur:
            row = await cur.fetchone()

    if not row:
        await update.message.reply_text(f"No open trade found with ID {trade_id}.")
        return

    trade = dict(row)

    await update.message.reply_text(
        f"⏳ Closing {trade['underlying']} {trade['strategy']} ({order_type})..."
    )

    try:
        await bot.execution_engine.close_position(trade, order_type=order_type, reason="manual")
        await update.message.reply_text(
            f"Close order submitted for {trade['underlying']} {trade['strategy']}.\n"
            "You'll be notified on fill."
        )
    except Exception as exc:
        logger.error("close_position failed for trade %s: %s", trade_id, exc, exc_info=True)
        await update.message.reply_text(f"❌ Close failed: {exc}")


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
    daily  = _e(f"{cfg.risk.daily_loss_limit_pct * 100:.1f}")
    weekly = _e(f"{cfg.risk.weekly_loss_limit_pct * 100:.1f}")
    monthly = _e(f"{cfg.risk.monthly_loss_limit_pct * 100:.1f}")
    delta  = _e(cfg.trading.csp.target_delta)
    max_loss = _e(f"{cfg.risk.max_spread_loss:g}")
    text = (
        f"*Current Configuration*\n"
        f"Automation: L{cfg.automation.level}\n"
        f"Buckets: Core {cfg.risk.core_bucket_pct*100:.0f}% / "
        f"Tactical {cfg.risk.tactical_bucket_pct*100:.0f}% / "
        f"Momentum {cfg.risk.momentum_bucket_pct*100:.0f}% / "
        f"Reserve {cfg.risk.reserve_pct*100:.0f}%\n"
        f"Daily stop: {daily}%  Weekly: {weekly}%  Monthly: {monthly}%\n"
        f"IVR min: Core {cfg.risk.min_ivr_core} / Tactical {cfg.risk.min_ivr_tactical}\n"
        f"CSP delta target: {delta}  DTE: {cfg.trading.csp.dte_min}–{cfg.trading.csp.dte_max}\n"
        f"Spread width: ${cfg.trading.spread.spread_width}  Max loss: ${max_loss}\n"
        f"LEAP stop: {cfg.leap.stop_loss_pct*100:.0f}%  Target: {cfg.leap.profit_target_pct*100:.0f}%\n"
    )
    await update.message.reply_text(text, parse_mode="MarkdownV2")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show current watchlist."""
    core = _e(", ".join(bot.config.scanner.watchlist.core))
    tactical = _e(", ".join(bot.config.scanner.watchlist.tactical))
    momentum = _e(", ".join(bot.config.leap.momentum_watchlist))
    text = f"*Watchlist*\nCore: {core}\nTactical: {tactical}\nMomentum: {momentum}"
    await update.message.reply_text(text, parse_mode="MarkdownV2")


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
