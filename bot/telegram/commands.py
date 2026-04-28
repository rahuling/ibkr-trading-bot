"""
M3: Telegram command handlers.

All /command implementations. Registered with the Application in bot.py.

PRD reference: §5 M3 Telegram Interface — Commands table.
"""

import asyncio
import json
import logging
import math
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
    Reconstruct an IBKR contract and fetch a FRESH limit price via reqMktData.

    Using proposal["credit_per_share"] as the limit price is stale (up to 2 hrs
    old). Instead we fetch the current bid-ask mid at approve-time so the order
    price reflects live market conditions.

    Returns (contract, price).
    """
    async def _fresh_mid(contract):
        td = bot.ibkr.ib.reqMktData(contract, genericTickList="", snapshot=False)
        await asyncio.sleep(3)
        bot.ibkr.ib.cancelMktData(contract)
        bid = td.bid if td.bid and not math.isnan(td.bid) and td.bid > 0 else None
        ask = td.ask if td.ask and not math.isnan(td.ask) and td.ask > 0 else None
        if bid is None or ask is None:
            raise RuntimeError(
                f"Cannot get fresh market data for {proposal_data['underlying']} — try again"
            )
        return round((bid + ask) / 2, 2)

    if strategy == "CSP":
        from ib_async import Option
        expiry_str = proposal_data["expiry"].replace("-", "")
        contract = Option(
            proposal_data["underlying"], expiry_str, proposal_data["strike"], "P", "SMART"
        )
        [qualified] = await bot.ibkr.ib.qualifyContractsAsync(contract)
        price = await _fresh_mid(qualified)
        return qualified, price

    if strategy == "CoveredCall":
        from ib_async import Option
        expiry_str = proposal_data["expiry"].replace("-", "")
        contract = Option(
            proposal_data["underlying"], expiry_str, proposal_data["strike"], "C", "SMART"
        )
        [qualified] = await bot.ibkr.ib.qualifyContractsAsync(contract)
        price = await _fresh_mid(qualified)
        return qualified, price

    if strategy == "BullPutSpread":
        from bot.builder.spread import build_bag_contract
        contract = build_bag_contract(
            bot.ibkr,
            proposal_data["underlying"],
            proposal_data["short_put_con_id"],
            proposal_data["long_put_con_id"],
        )
        price = await _fresh_mid(contract)
        return contract, price

    if strategy == "LEAPCall":
        from ib_async import Option
        expiry_str = proposal_data["expiry"].replace("-", "")
        contract = Option(
            proposal_data["underlying"], expiry_str, proposal_data["strike"], "C", "SMART"
        )
        [qualified] = await bot.ibkr.ib.qualifyContractsAsync(contract)
        price = await _fresh_mid(qualified)
        return qualified, price

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

async def _scan_for_cc_proposals(update, bot) -> None:
    """
    M6: Generate CC proposals for assigned Wheel positions that don't yet have an open CC.

    Called from cmd_scan so /scan covers the full Wheel cycle, not just CSP/Spread.
    """
    import uuid
    from datetime import datetime, timedelta, timezone

    from bot.builder.cc import build_cc_proposal, format_cc_trade_card

    async with get_db() as db:
        async with db.execute(
            """SELECT wc.underlying,
                      t.entry_credit AS csp_credit,
                      t.legs         AS csp_legs
               FROM wheel_cycles wc
               JOIN trades t ON t.wheel_cycle_id = wc.cycle_id
                            AND t.strategy = 'CSP'
               WHERE wc.status = 'open'
                 AND wc.shares_assigned = 1
                 AND NOT EXISTS (
                     SELECT 1 FROM trades cc
                     WHERE cc.wheel_cycle_id = wc.cycle_id
                       AND cc.strategy = 'CoveredCall'
                       AND cc.status = 'open'
                 )"""
        ) as cur:
            assigned_rows = await cur.fetchall()

    if not assigned_rows:
        return

    await update.message.reply_text(
        f"📋 Found {len(assigned_rows)} assigned position(s) needing Covered Call..."
    )

    now = datetime.now(timezone.utc)
    generated = 0
    for row in assigned_rows:
        underlying = row["underlying"]
        csp_credit = row["csp_credit"] or 0
        csp_legs   = json.loads(row["csp_legs"]) if isinstance(row["csp_legs"], str) else row["csp_legs"]
        csp_leg    = next((l for l in csp_legs if l.get("action") == "SELL"), None)
        strike     = csp_leg.get("strike", 0) if csp_leg else 0
        net_cost   = round(strike - csp_credit, 2)

        try:
            proposal = await build_cc_proposal(bot.config, bot.ibkr, underlying, net_cost)
            if not proposal:
                await update.message.reply_text(
                    f"{underlying}: no CC proposal available (market data unavailable or no valid strike)."
                )
                continue

            proposal_id = uuid.uuid4().hex[:6].upper()
            trade_card  = format_cc_trade_card(proposal, proposal_id)

            async with get_db() as db:
                await db.execute(
                    """INSERT INTO proposals
                       (proposal_id, underlying, strategy, trade_card_json,
                        status, created_at, expires_at)
                       VALUES (?, ?, 'CoveredCall', ?, 'pending', ?, ?)""",
                    (
                        proposal_id,
                        underlying,
                        json.dumps(proposal.__dict__),
                        now.isoformat(),
                        (now + timedelta(hours=20)).isoformat(),
                    ),
                )
                await db.commit()

            await update.message.reply_text(trade_card)
            generated += 1
        except Exception as exc:
            logger.error("CC proposal generation failed for %s: %s", underlying, exc, exc_info=True)
            await update.message.reply_text(f"{underlying}: CC proposal error — {exc}")

    if generated:
        await update.message.reply_text(f"✅ {generated} CC proposal(s) generated.")


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
        return

    # M6: also generate CC proposals for any assigned positions without an active CC.
    await _scan_for_cc_proposals(update, bot)


async def cmd_positions(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show all open positions with current P&L and Greeks."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "positions"):
        await update.message.reply_text("Please wait before refreshing positions.")
        return

    async with get_db() as db:
        async with db.execute(
            """SELECT t.*, p.current_value, p.unrealised_pnl, p.delta, p.theta, p.last_updated
               FROM trades t
               LEFT JOIN positions p ON t.trade_id = p.trade_id
               WHERE t.status = 'open'
               ORDER BY t.entry_date""",
        ) as cur:
            rows = await cur.fetchall()

    if not rows:
        await update.message.reply_text("No open positions.")
        return

    lines = [f"📈 OPEN POSITIONS ({len(rows)})", "──────────────────────"]
    for row in rows:
        t = dict(row)
        strat    = t["strategy"]
        ticker   = t["underlying"]
        legs     = json.loads(t["legs"]) if isinstance(t["legs"], str) else t["legs"]
        entry    = t.get("entry_credit") or 0
        trade_id = t["trade_id"]

        # Build header line from leg data
        short = next((l for l in legs if l.get("action") == "SELL"), None)
        long  = next((l for l in legs if l.get("action") == "BUY"), None)
        expiry_short = (short["expiry"][5:] if short else "?")  # "MM-DD"

        if strat == "CSP":
            header = f"{ticker} CSP  ${short['strike']:.0f}P  {expiry_short}"
        elif strat == "BullPutSpread":
            header = (f"{ticker} BPS  ${short['strike']:.0f}/${long['strike']:.0f}  {expiry_short}"
                      if short and long else f"{ticker} BPS  {expiry_short}")
        elif strat == "LEAPCall":
            exp = long["expiry"][5:] if long else "?"
            header = (f"{ticker} LEAP ${long['strike']:.0f}C  {exp}" if long
                      else f"{ticker} LEAP  {expiry_short}")
        elif strat == "CoveredCall":
            header = f"{ticker} CC  ${short['strike']:.0f}C  {expiry_short}" if short else f"{ticker} CC  {expiry_short}"
        else:
            header = f"{ticker} {strat}  {expiry_short}"

        lines.append(f"\n[{header}]")
        cost_label = "cost" if strat == "LEAPCall" else "credit"
        lines.append(f"  Entry: ${entry:.2f} {cost_label}")

        unreal = t.get("unrealised_pnl")
        if unreal is not None:
            sign = "+" if unreal >= 0 else ""
            pct  = unreal / (entry * 100) * 100 if entry else 0
            lines.append(f"  P&L:   {sign}${unreal:.0f}  ({sign}{pct:.0f}%)  [live]")
            delta = t.get("delta")
            theta = t.get("theta")
            if delta is not None or theta is not None:
                greek_parts = []
                if delta is not None:
                    greek_parts.append(f"Δ {delta:.2f}")
                if theta is not None:
                    greek_parts.append(f"Θ ${theta:.2f}/d")
                lines.append(f"  {' | '.join(greek_parts)}")
        else:
            lines.append("  P&L:   no live data (run /scan to start monitoring)")

        lines.append(f"  ID: {trade_id}")

    lines.append("\n/close <id>  /roll <id>")
    await update.message.reply_text("\n".join(lines))


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

        # Risk gate: check all limits before submitting
        if bot.risk_engine:
            if strategy in ("CSP", "CoveredCall"):
                capital = proposal_data.get("capital_required", 0) or 0
                bucket = "Core"
            elif strategy == "LEAPCall":
                capital = proposal_data.get("cost_total", 0) or 0
                bucket = "Momentum"
            else:  # BullPutSpread
                capital = (proposal_data.get("spread_width", 0) or 0) * 100
                bucket = "Tactical"
            risk = await bot.risk_engine.check_new_trade(underlying, capital or 0, bucket)
            if risk.result.value == "blocked":
                await update.message.reply_text(f"❌ Risk check blocked: {risk.reason}")
                return
            if risk.result.value == "warning":
                await update.message.reply_text(f"⚠️ Risk warning: {risk.reason}\nProceeding...")

        try:
            contract, price = await _build_contract_for_approval(bot, strategy, proposal_data)
        except Exception as exc:
            logger.error("Contract reconstruction failed for %s: %s", proposal_id, exc)
            await update.message.reply_text(f"❌ Failed to reconstruct contract: {exc}")
            return

        await update.message.reply_text(
            f"⏳ Submitting {underlying} {strategy} at ${price:.2f}..."
        )

        # LEAP calls are BUY orders; all premium-selling strategies are SELL orders.
        order_action = "BUY" if strategy == "LEAPCall" else "SELL"

        try:
            client_order_id = await bot.execution_engine.submit_order(
                db, contract, price, quantity=1, proposal_id=proposal_id, action=order_action
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
    """Show roll economics for an open CSP position."""
    args = context.args if context.args else []
    if not args:
        await update.message.reply_text("Usage: /roll <trade_id>")
        return

    trade_id = args[0]

    if not bot.position_manager:
        await update.message.reply_text("Position manager not ready.")
        return

    await update.message.reply_text("⏳ Fetching roll proposal...")

    async with get_db() as db:
        try:
            proposal = await bot.position_manager.build_roll_proposal(db, trade_id)
        except Exception as exc:
            logger.error("build_roll_proposal failed: %s", exc, exc_info=True)
            await update.message.reply_text(f"Roll analysis failed: {exc}")
            return

    if proposal is None:
        await update.message.reply_text(
            f"No roll available for {trade_id} — check that the position is open and "
            "market data is accessible."
        )
        return

    if "error" in proposal:
        await update.message.reply_text(proposal["error"])
        return

    sign = "+" if proposal["net_credit"] >= 0 else ""
    debit_note = "  ⚠️ DEBIT ROLL — requires explicit approval" if proposal["is_debit"] else ""
    text = (
        f"📋 ROLL PROPOSAL — {proposal['underlying']}\n"
        f"──────────────────────\n"
        f"Current:  ${proposal['current_strike']:.0f}P  {proposal['current_expiry']}\n"
        f"Roll to:  ${proposal['roll_strike']:.0f}P  {proposal['roll_expiry']} "
        f"({proposal['roll_dte']} DTE)\n\n"
        f"Close debit:  ${proposal['close_debit']:.2f}\n"
        f"New credit:   ${proposal['new_credit']:.2f}\n"
        f"Net:          {sign}${proposal['net_credit']:.2f} "
        f"({sign}${proposal['net_credit_total']:.0f} total){debit_note}\n\n"
        f"To execute:\n"
        f"  1. /close {trade_id}\n"
        f"  2. /scan  (to generate new proposal at rolled strike)\n"
        f"  3. /approve <new_proposal_id>"
    )
    await update.message.reply_text(text)


async def cmd_risk(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show the risk dashboard: capital allocation, P&L limits, PDT status."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "risk"):
        await update.message.reply_text("Please wait before refreshing risk data.")
        return

    if not bot.risk_engine:
        await update.message.reply_text("Risk engine not ready.")
        return

    try:
        text = await bot.risk_engine.format_risk_dashboard()
    except Exception as exc:
        logger.error("format_risk_dashboard failed: %s", exc, exc_info=True)
        await update.message.reply_text(f"Risk dashboard error: {exc}")
        return

    await update.message.reply_text(text)


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
    """Show trade journal for the last N days (default 30)."""
    args = context.args if context.args else []
    days = 30
    if args:
        try:
            days = int(args[0])
            if days < 1 or days > 365:
                await update.message.reply_text("Days must be between 1 and 365.")
                return
        except ValueError:
            await update.message.reply_text("Usage: /journal [days]  (e.g. /journal 60)")
            return

    from bot.journal.journal import Journal
    journal = Journal(bot.config)

    async with get_db() as db:
        try:
            text = await journal.get_journal(db, days)
        except Exception as exc:
            logger.error("get_journal failed: %s", exc, exc_info=True)
            await update.message.reply_text(f"Journal error: {exc}")
            return

    await update.message.reply_text(text)


async def cmd_wheel(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show full Wheel cycle P&L. Usage: /wheel [cycle_id | trade_id]"""
    args = context.args if context.args else []
    id_arg = args[0] if args else ""

    from bot.journal.journal import Journal
    journal = Journal(bot.config)

    async with get_db() as db:
        try:
            text = await journal.get_wheel_cycle(db, id_arg)
        except Exception as exc:
            logger.error("get_wheel_cycle failed: %s", exc, exc_info=True)
            await update.message.reply_text(f"Wheel error: {exc}")
            return

    await update.message.reply_text(text)


async def cmd_analyze(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Show win-rate analytics. Usage: /analyze [ivr | dte | strategy | <rule_tag>]"""
    args = context.args if context.args else []
    tag = args[0] if args else ""

    from bot.journal.journal import Journal
    journal = Journal(bot.config)

    async with get_db() as db:
        try:
            text = await journal.analyze_by_tag(db, tag)
        except Exception as exc:
            logger.error("analyze_by_tag failed: %s", exc, exc_info=True)
            await update.message.reply_text(f"Analyze error: {exc}")
            return

    await update.message.reply_text(text)


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

    # Parse and apply value, write audit log
    section, field = param.split(".", 1)
    section_obj = getattr(bot.config, section, None)
    if section_obj is None:
        await update.message.reply_text(f"Unknown config section: {section}")
        return

    old_value = getattr(section_obj, field, None)
    if old_value is None:
        await update.message.reply_text(f"Unknown config field: {field}")
        return

    # Cast to the same type as the existing value
    try:
        if isinstance(old_value, bool):
            new_value = value.lower() in ("true", "1", "yes")
        elif isinstance(old_value, int):
            new_value = int(value)
        elif isinstance(old_value, float):
            new_value = float(value)
        else:
            new_value = value
    except (ValueError, TypeError) as exc:
        await update.message.reply_text(f"Invalid value '{value}' for {param}: {exc}")
        return

    setattr(section_obj, field, new_value)

    async with get_db() as db:
        await db.execute(
            """INSERT INTO config_changes (param, old_value, new_value, changed_by, changed_at)
               VALUES (?, ?, ?, 'user', ?)""",
            (param, str(old_value), str(new_value), datetime.now(timezone.utc).isoformat()),
        )
        await db.commit()

    logger.info("Config changed: %s  %s → %s", param, old_value, new_value)
    await update.message.reply_text(
        f"✅ Config updated: {param}\n{old_value} → {new_value}"
    )


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
    """Force state reconciliation: compare DB open positions against live IBKR positions."""
    user_id = update.effective_user.id
    if not _check_rate_limit(user_id, "reconcile"):
        await update.message.reply_text("Reconciliation already in progress — please wait.")
        return

    if not bot.ibkr or not bot.ibkr.is_connected:
        await update.message.reply_text("IB Gateway not connected — cannot reconcile.")
        return

    await update.message.reply_text("⏳ Running reconciliation...")

    # 1. Recover any pending_submit orphans
    orphan_count = 0
    if bot.execution_engine:
        async with get_db() as db:
            async with db.execute(
                "SELECT COUNT(*) FROM orders WHERE status = 'pending_submit'"
            ) as cur:
                row = await cur.fetchone()
            orphan_count = row[0] if row else 0
            if orphan_count:
                await bot.execution_engine.recover_orphaned_orders(db)

    # 2. Compare DB open trades vs IBKR live positions (options only)
    async with get_db() as db:
        async with db.execute(
            "SELECT underlying, strategy, legs FROM trades WHERE status = 'open'"
        ) as cur:
            db_rows = await cur.fetchall()

    db_underlyings = {row["underlying"] for row in db_rows}

    try:
        ibkr_positions = bot.ibkr.ib.positions()
        ibkr_underlyings = {
            p.contract.symbol
            for p in ibkr_positions
            if p.contract.secType in ("OPT", "BAG")
        }
    except Exception as exc:
        await update.message.reply_text(f"IBKR position query failed: {exc}")
        return

    in_db_not_ibkr = db_underlyings - ibkr_underlyings
    in_ibkr_not_db = ibkr_underlyings - db_underlyings

    lines = ["🔄 Reconciliation complete"]
    if orphan_count:
        lines.append(f"  Orphaned orders checked: {orphan_count}")
    if not in_db_not_ibkr and not in_ibkr_not_db:
        lines.append("  ✅ DB and IBKR positions match")
    else:
        if in_db_not_ibkr:
            lines.append(f"  ⚠️ In DB but not IBKR: {', '.join(sorted(in_db_not_ibkr))}")
        if in_ibkr_not_db:
            lines.append(f"  ⚠️ In IBKR but not DB: {', '.join(sorted(in_ibkr_not_db))}")
        lines.append("  Review manually and use /close to correct DB state if needed.")

    await update.message.reply_text("\n".join(lines))
