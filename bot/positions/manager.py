"""
M5: Position Manager.

Tracks all open positions, monitors Greeks and P&L, triggers management
rules, and tracks Wheel cycles.

Price monitoring is EVENT-DRIVEN via ib_async tick subscriptions —
not polled on a 5-minute timer. Strike-tested alerts fire in seconds,
not at the next poll cycle.

The 5-minute check_all_positions() job supplements event-driven monitoring:
  - Updates positions table (current_value, unrealised_pnl, Greeks)
  - Fires profit-target alerts missed during reconnect windows
  - Subscribes to any new positions not yet monitored

PRD reference: §5 M5 Position Manager.
"""

import asyncio
import json
import logging
import math
import uuid
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)


class PositionManager:
    """
    Manages open position state and event-driven price monitoring.
    """

    def __init__(self, config, ibkr, risk_engine):
        self.config = config
        self.ibkr = ibkr
        self.risk = risk_engine
        self.on_alert = None              # set by TelegramBot.wire()
        self._execution_engine = None     # set via set_execution_engine()
        self._subscriptions: Dict[str, tuple] = {}   # underlying → (ticker, contract, handler)
        self._alerted: set = set()        # (trade_id, alert_type) — deduplication

    def set_execution_engine(self, execution_engine) -> None:
        self._execution_engine = execution_engine

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def subscribe_all_open_positions(self) -> None:
        """
        On startup: subscribe to underlying price tick events for every open position.

        Called once after state reconciliation. Subsequent new positions are
        picked up by check_all_positions() on its first run after the fill.
        """
        from bot.database import get_db

        async with get_db() as db:
            async with db.execute("SELECT * FROM trades WHERE status = 'open'") as cur:
                rows = await cur.fetchall()

        logger.info("subscribe_all_open_positions: %d open trade(s)", len(rows))
        for row in rows:
            position = dict(row)
            if position["underlying"] not in self._subscriptions:
                await self.subscribe_position(position)

    def setup_assignment_detection(self) -> None:
        """
        Subscribe to IBKR positionEvent to detect option assignments.

        Fires whenever the broker account position for any contract changes.
        We look for a short put going to 0 while stock appears → assignment.
        Called once at startup after state reconciliation.
        """
        self.ibkr.ib.positionEvent += self._on_ibkr_position_update
        logger.info("Assignment detection wired to positionEvent")

    def _on_ibkr_position_update(self, ibkr_pos) -> None:
        asyncio.create_task(self._check_assignment(ibkr_pos))

    async def _check_assignment(self, ibkr_pos) -> None:
        """Detect assignment: short put goes to 0 AND stock appears in account."""
        contract = ibkr_pos.contract
        if contract.secType != "OPT" or ibkr_pos.position != 0:
            return

        from bot.database import get_db
        async with get_db() as db:
            async with db.execute(
                "SELECT * FROM trades WHERE underlying = ? AND strategy = 'CSP' AND status = 'open'",
                (contract.symbol,),
            ) as cur:
                rows = await cur.fetchall()

        for row in rows:
            position = dict(row)
            legs = json.loads(position["legs"])
            leg = legs[0]
            expiry_compact = contract.lastTradeDateOrContractMonth.replace("-", "")
            if (
                abs(float(leg["strike"]) - float(contract.strike)) < 0.01
                and leg["expiry"].replace("-", "") == expiry_compact
            ):
                ibkr_positions = self.ibkr.ib.positions()
                has_stock = any(
                    p.contract.symbol == contract.symbol
                    and p.contract.secType == "STK"
                    and p.position >= 100
                    for p in ibkr_positions
                )
                if has_stock:
                    async with get_db() as db:
                        await self.handle_assignment(db, position)
                break

    # ------------------------------------------------------------------
    # Event-driven price monitoring
    # ------------------------------------------------------------------

    async def subscribe_position(self, position, on_alert: Callable = None) -> None:
        """
        Subscribe to real-time underlying stock price ticks for a position.

        Registers an updateEvent handler that fires on_price_update on every
        price change. Idempotent — silently skips if already subscribed.
        """
        from ib_async import Stock

        underlying = position["underlying"]
        if underlying in self._subscriptions:
            return

        stock = Stock(underlying, "SMART", "USD")
        try:
            [qualified] = await self.ibkr.ib.qualifyContractsAsync(stock)
        except Exception as exc:
            logger.error("Cannot qualify %s for price monitoring: %s", underlying, exc)
            return

        ticker = self.ibkr.ib.reqMktData(qualified, genericTickList="", snapshot=False)
        alert_fn = on_alert or self.on_alert

        # Capture position in closure; create_task so handler stays non-blocking
        def _handler(t):
            asyncio.create_task(self.on_price_update(t, position, alert_fn))

        ticker.updateEvent += _handler
        self._subscriptions[underlying] = (ticker, qualified, _handler)
        logger.info("Subscribed to %s price ticks (trade=%s)", underlying, position.get("trade_id", "?")[:8])

    def unsubscribe_position(self, position) -> None:
        """Cancel price tick subscription when a position is closed."""
        underlying = position.get("underlying")
        if not underlying or underlying not in self._subscriptions:
            return
        ticker, contract, handler = self._subscriptions.pop(underlying)
        ticker.updateEvent -= handler
        self.ibkr.ib.cancelMktData(contract)
        logger.info("Unsubscribed from %s price ticks", underlying)
        trade_id = position.get("trade_id")
        if trade_id:
            self._alerted = {k for k in self._alerted if k[0] != trade_id}

    async def on_price_update(self, ticker, position, on_alert: Callable = None) -> None:
        """
        Callback fired on every underlying price tick for a monitored position.

        Checks:
          - CSP/Spread: is underlying within 2% of short strike?
          - LEAP: has underlying hit stop_price or profit_target_price?
        """
        price = ticker.last
        if price is None or (isinstance(price, float) and math.isnan(price)):
            price = ticker.close
        if price is None or (isinstance(price, float) and math.isnan(price)) or price <= 0:
            return

        strategy = position.get("strategy", "")
        trade_id = position.get("trade_id", "")
        alert_fn = on_alert or self.on_alert

        # CSP / BullPutSpread: strike-tested alert
        if strategy in ("CSP", "BullPutSpread"):
            legs = json.loads(position["legs"]) if isinstance(position["legs"], str) else position["legs"]
            short_leg = next((l for l in legs if l.get("action") == "SELL"), None)
            if short_leg:
                strike = short_leg.get("strike", 0)
                pct = abs(price - strike) / strike if strike else 1.0
                alert_key = (trade_id, "strike_tested")
                if pct <= 0.02 and alert_key not in self._alerted:
                    self._alerted.add(alert_key)
                    if alert_fn:
                        await alert_fn(
                            f"⚠️ STRIKE TESTED: {position['underlying']} at ${price:.2f} "
                            f"({pct * 100:.1f}% from ${strike:.0f} strike)\n"
                            f"Trade: {trade_id[:8]}..."
                        )
                elif pct > 0.05:
                    # Price recovered — clear so the alert can fire again next time
                    self._alerted.discard(alert_key)

        # CoveredCall: alert when stock trades at or above the call strike (assignment risk)
        elif strategy == "CoveredCall":
            legs = json.loads(position["legs"]) if isinstance(position["legs"], str) else position["legs"]
            short_leg = next((l for l in legs if l.get("action") == "SELL"), None)
            if short_leg:
                strike = short_leg.get("strike", 0)
                itm_key = (trade_id, "cc_itm")
                if strike and price >= strike and itm_key not in self._alerted:
                    self._alerted.add(itm_key)
                    if alert_fn:
                        await alert_fn(
                            f"📈 CC IN-THE-MONEY: {position['underlying']} at ${price:.2f} "
                            f"(above ${strike:.0f} call — stock may be called away)\n"
                            f"Trade: {trade_id[:8]}..."
                        )
                elif strike and price < strike * 0.98:
                    self._alerted.discard(itm_key)

        # LEAP Calls: stop-loss / profit-target monitoring (underlying price-based)
        elif strategy == "LEAPCall":
            stop_price   = position.get("stop_price")
            target_price = position.get("profit_target_price")

            if stop_price and price <= stop_price:
                stop_key = (trade_id, "leap_stop")
                if stop_key not in self._alerted:
                    self._alerted.add(stop_key)
                    await self.trigger_leap_stop_loss(position, price)

            elif target_price and price >= target_price:
                target_key = (trade_id, "leap_target")
                if target_key not in self._alerted:
                    self._alerted.add(target_key)
                    await self.trigger_leap_profit_target(position, price)

    # ------------------------------------------------------------------
    # Management triggers
    # ------------------------------------------------------------------

    async def check_all_positions(self) -> None:
        """
        5-minute fallback poll: update positions table and check management rules.

        Also subscribes to any newly filled positions not yet monitored.
        """
        from bot.database import get_db

        async with get_db() as db:
            async with db.execute("SELECT * FROM trades WHERE status = 'open'") as cur:
                rows = await cur.fetchall()

        if not rows:
            return

        logger.debug("check_all_positions: %d open position(s)", len(rows))

        for row in rows:
            position = dict(row)
            underlying = position["underlying"]

            # Subscribe if not yet monitored (handles positions filled since startup)
            if underlying not in self._subscriptions:
                await self.subscribe_position(position)

            try:
                await self._check_and_update_position(position)
            except Exception as exc:
                logger.error(
                    "check_all_positions error for trade=%s: %s",
                    position.get("trade_id", "?")[:8], exc,
                )

    async def _check_and_update_position(self, position) -> None:
        """Fetch current option price, update positions table, check profit target."""
        from bot.database import get_db
        from ib_async import Option

        trade_id      = position["trade_id"]
        underlying    = position["underlying"]
        strategy      = position["strategy"]
        entry_credit  = position.get("entry_credit") or 0.0
        legs = json.loads(position["legs"]) if isinstance(position["legs"], str) else position["legs"]

        # LEAP is a long call (BUY leg); all premium-selling strategies have a SELL leg.
        if strategy == "LEAPCall":
            active_leg = next((l for l in legs if l.get("action") == "BUY"), None)
        else:
            active_leg = next((l for l in legs if l.get("action") == "SELL"), None)
        if not active_leg:
            return

        expiry = active_leg["expiry"].replace("-", "")
        try:
            opt = Option(underlying, expiry, active_leg["strike"], active_leg.get("right", "P"), "SMART")
            [qualified] = await self.ibkr.ib.qualifyContractsAsync(opt)
            td = self.ibkr.ib.reqMktData(qualified, genericTickList="", snapshot=False)
            await asyncio.sleep(3)
            self.ibkr.ib.cancelMktData(qualified)
        except Exception as exc:
            logger.warning("Market data unavailable for %s %s: %s", underlying, strategy, exc)
            return

        bid = td.bid if td.bid and not math.isnan(td.bid) and td.bid > 0 else None
        ask = td.ask if td.ask and not math.isnan(td.ask) and td.ask > 0 else None
        if bid is None or ask is None:
            return
        mid = (bid + ask) / 2

        # Greeks from model
        delta = theta = None
        if td.modelGreeks:
            g = td.modelGreeks
            delta = g.delta if g.delta and not math.isnan(g.delta) else None
            theta = g.theta if g.theta and not math.isnan(g.theta) else None

        # LEAP: bought at entry_credit, profit when mid rises.
        # Premium-selling: sold at entry_credit, profit when mid falls.
        if strategy == "LEAPCall":
            unrealised_pnl = round((mid - entry_credit) * 100, 2)
        else:
            unrealised_pnl = round((entry_credit - mid) * 100, 2)
        current_value  = round(mid * 100, 2)
        now = datetime.now(timezone.utc)

        async with get_db() as db:
            await db.execute(
                """INSERT INTO positions
                   (position_id, trade_id, underlying, current_value,
                    unrealised_pnl, delta, theta, last_updated)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(position_id) DO UPDATE SET
                     current_value  = excluded.current_value,
                     unrealised_pnl = excluded.unrealised_pnl,
                     delta          = excluded.delta,
                     theta          = excluded.theta,
                     last_updated   = excluded.last_updated""",
                (trade_id, trade_id, underlying, current_value,
                 unrealised_pnl, delta, theta, now.isoformat()),
            )
            await db.commit()

        # LEAP exits use underlying price alerts (on_price_update), not option mid.
        if strategy == "LEAPCall":
            return

        # Profit-target alert (strategy-specific threshold)
        if strategy == "CSP":
            profit_close_pct = self.config.trading.csp.profit_close_pct
        elif strategy == "BullPutSpread":
            profit_close_pct = self.config.trading.spread.profit_close_pct
        elif strategy == "CoveredCall":
            profit_close_pct = self.config.trading.covered_call.profit_close_pct
        else:
            profit_close_pct = None

        if profit_close_pct and entry_credit > 0:
            profit_threshold_dollars = entry_credit * profit_close_pct * 100
            alert_key = (trade_id, "profit_target")
            if unrealised_pnl >= profit_threshold_dollars and alert_key not in self._alerted:
                self._alerted.add(alert_key)
                pct_of_credit = unrealised_pnl / (entry_credit * 100) * 100
                if self.on_alert:
                    await self.on_alert(
                        f"📈 PROFIT TARGET: {underlying} {strategy}\n"
                        f"P&L: +${unrealised_pnl:.0f} ({pct_of_credit:.0f}% of credit)\n"
                        f"Consider closing: /close {trade_id}"
                    )

    # ------------------------------------------------------------------
    # LEAP stop/target execution
    # ------------------------------------------------------------------

    async def trigger_leap_stop_loss(self, position, current_underlying_price: float) -> None:
        """
        Close a LEAP position because underlying hit stop_price.

        Order priority: limit at mid → bid urgency (automatic via close_position).
        """
        underlying  = position.get("underlying", "?")
        stop_price  = position.get("stop_price")

        logger.warning(
            "LEAP stop triggered: %s at %.2f (stop=%.2f)",
            underlying, current_underlying_price, stop_price or 0,
        )

        if self.on_alert:
            await self.on_alert(
                f"⛔ LEAP STOP LOSS: {underlying} at ${current_underlying_price:.2f} "
                f"(stop: ${stop_price:.2f}). Submitting close order."
            )

        if self._execution_engine:
            try:
                await self._execution_engine.close_position(
                    position, order_type="bid", reason="stop_loss"
                )
            except Exception as exc:
                logger.error("LEAP stop-loss close failed: %s", exc)
                if self.on_alert:
                    await self.on_alert(
                        f"⚠️ LEAP stop-loss close FAILED for {underlying}: {exc}\n"
                        "Close manually in TWS immediately!"
                    )

    async def trigger_leap_profit_target(self, position, current_underlying_price: float) -> None:
        """Close a LEAP position because underlying hit profit_target_price."""
        underlying    = position.get("underlying", "?")
        target_price  = position.get("profit_target_price")

        logger.info(
            "LEAP profit target triggered: %s at %.2f (target=%.2f)",
            underlying, current_underlying_price, target_price or 0,
        )

        if self.on_alert:
            await self.on_alert(
                f"📈 LEAP PROFIT TARGET: {underlying} at ${current_underlying_price:.2f} "
                f"(target: ${target_price:.2f}). Submitting close order."
            )

        if self._execution_engine:
            try:
                await self._execution_engine.close_position(
                    position, order_type="limit", reason="profit_target"
                )
            except Exception as exc:
                logger.error("LEAP profit-target close failed: %s", exc)
                if self.on_alert:
                    await self.on_alert(
                        f"⚠️ LEAP profit-target close FAILED for {underlying}: {exc}"
                    )

    # ------------------------------------------------------------------
    # Wheel cycle management
    # ------------------------------------------------------------------

    async def create_wheel_cycle(self, db, underlying: str, csp_trade_id: str) -> str:
        """
        Create a new wheel_cycle record when a CSP is entered on a Core ticker.

        Returns the new cycle_id.
        """
        cycle_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        await db.execute(
            """INSERT INTO wheel_cycles (cycle_id, underlying, started_at, status)
               VALUES (?, ?, ?, 'open')""",
            (cycle_id, underlying, now.isoformat()),
        )
        await db.execute(
            "UPDATE trades SET wheel_cycle_id = ? WHERE trade_id = ?",
            (cycle_id, csp_trade_id),
        )
        await db.commit()

        logger.info("Wheel cycle created: %s for %s", cycle_id, underlying)
        return cycle_id

    async def link_cc_to_wheel_cycle(self, db, underlying: str, cc_trade_id: str) -> None:
        """Link a filled CoveredCall trade to the most recent open wheel cycle for underlying."""
        async with db.execute(
            """SELECT cycle_id FROM wheel_cycles
               WHERE underlying = ? AND status = 'open' AND shares_assigned = 1
               ORDER BY started_at DESC LIMIT 1""",
            (underlying,),
        ) as cur:
            row = await cur.fetchone()
        if row:
            cycle_id = row["cycle_id"]
            await db.execute(
                "UPDATE trades SET wheel_cycle_id = ? WHERE trade_id = ?",
                (cycle_id, cc_trade_id),
            )
            await db.commit()
            logger.info("Linked CoveredCall %s to wheel cycle %s", cc_trade_id, cycle_id)
        else:
            logger.warning(
                "No assigned open wheel cycle for %s — CoveredCall %s not linked",
                underlying, cc_trade_id,
            )

    async def handle_assignment(self, db, position, on_alert: Callable = None) -> None:
        """
        Called when IBKR account update shows assignment (long stock appears,
        short put disappears).

        Steps:
          1. Mark CSP trade: status=closed, outcome=Assigned
          2. Update wheel_cycle: shares_assigned=True
          3. Cancel underlying tick subscription (position is closed)
          4. Alert user via Telegram
        """
        trade_id   = position.get("trade_id")
        underlying = position.get("underlying", "?")
        legs = json.loads(position["legs"]) if isinstance(position["legs"], str) else position["legs"]

        short_leg     = next((l for l in legs if l.get("action") == "SELL"), None)
        strike        = short_leg.get("strike", 0) if short_leg else 0
        entry_credit  = position.get("entry_credit", 0) or 0
        net_cost      = round(strike - entry_credit, 2)

        now = datetime.now(timezone.utc)

        pnl = round(entry_credit * 100, 2)  # premium collected is kept on assignment
        await db.execute(
            """UPDATE trades
               SET status = 'closed', exit_date = ?, exit_reason = 'assignment',
                   outcome = 'Assigned', exit_price = 0, pnl = ?
               WHERE trade_id = ?""",
            (now.isoformat(), pnl, trade_id),
        )

        cycle_id = position.get("wheel_cycle_id")
        if cycle_id:
            await db.execute(
                "UPDATE wheel_cycles SET shares_assigned = 1 WHERE cycle_id = ?",
                (cycle_id,),
            )

        await db.execute("DELETE FROM positions WHERE trade_id = ?", (trade_id,))
        await db.commit()

        self.unsubscribe_position(position)

        alert_fn = on_alert or self.on_alert
        if alert_fn:
            await alert_fn(
                f"📉 ASSIGNED: 100 shares of {underlying} received.\n"
                f"Net cost basis: ${net_cost:.2f}/share  (strike ${strike:.0f} − credit ${entry_credit:.2f})\n"
                f"Building Covered Call proposal..."
            )

        logger.info("Assignment handled: %s trade=%s", underlying, trade_id)

        # Generate a Covered Call proposal immediately if market data is available,
        # otherwise fall back to alerting the user to run /scan next morning.
        try:
            import uuid
            import json as _json
            from datetime import timedelta
            from bot.builder.cc import build_cc_proposal, format_cc_trade_card
            from bot.database import get_db as _get_db

            cc_proposal = await build_cc_proposal(self.config, self.ibkr, underlying, net_cost)
            if cc_proposal:
                proposal_id = uuid.uuid4().hex[:6].upper()
                trade_card = format_cc_trade_card(cc_proposal, proposal_id)
                now_utc = datetime.now(timezone.utc)
                expires_at = now_utc + timedelta(hours=20)

                async with _get_db() as cc_db:
                    await cc_db.execute(
                        """INSERT INTO proposals
                           (proposal_id, underlying, strategy, trade_card_json,
                            status, created_at, expires_at)
                           VALUES (?, ?, 'CoveredCall', ?, 'pending', ?, ?)""",
                        (
                            proposal_id,
                            underlying,
                            _json.dumps(cc_proposal.__dict__),
                            now_utc.isoformat(),
                            expires_at.isoformat(),
                        ),
                    )
                    await cc_db.commit()

                if alert_fn:
                    await alert_fn(trade_card)
            else:
                if alert_fn:
                    await alert_fn(
                        f"📋 Covered Call proposal: market data unavailable now.\n"
                        f"Run /scan tomorrow morning to generate a CC proposal for {underlying}."
                    )
        except Exception as exc:
            logger.error("CC proposal generation failed after %s assignment: %s", underlying, exc)
            if alert_fn:
                await alert_fn(
                    f"⚠️ Could not generate Covered Call proposal for {underlying}: {exc}\n"
                    "Use /scan to generate it manually."
                )

    # ------------------------------------------------------------------
    # Roll logic
    # ------------------------------------------------------------------

    async def build_roll_proposal(self, db, trade_id: str) -> Optional[dict]:
        """
        Build a CSP roll proposal for /roll [id].

        Finds the next expiry after the current one, checks the same strike
        and 1–2 strikes lower (more OTM), and returns economics for the best
        net-credit option.

        Returns None if data is unavailable.
        Returns dict with "error" key if strategy not yet supported.
        """
        from ib_async import Option, Stock
        from datetime import date

        async with db.execute(
            "SELECT * FROM trades WHERE trade_id = ? AND status = 'open'", (trade_id,)
        ) as cur:
            row = await cur.fetchone()

        if not row:
            return None

        position = dict(row)
        strategy = position["strategy"]

        if strategy != "CSP":
            return {"error": f"Roll not yet supported for {strategy} — coming in Phase 5"}

        legs       = json.loads(position["legs"])
        underlying = position["underlying"]
        leg        = legs[0]
        cur_strike = leg["strike"]
        cur_expiry = leg["expiry"]  # "YYYY-MM-DD"

        # Get option chain for next expiry
        stock = Stock(underlying, "SMART", "USD")
        try:
            [qs] = await self.ibkr.ib.qualifyContractsAsync(stock)
        except Exception as exc:
            logger.error("build_roll_proposal qualify failed: %s", exc)
            return None

        chains = await self.ibkr.ib.reqSecDefOptParamsAsync(
            qs.symbol, "", qs.secType, qs.conId
        )
        if not chains:
            return None

        chain = chains[0]
        today          = date.today()
        cur_exp_date   = date.fromisoformat(cur_expiry)

        # Next expiry: after current expiry and at least 14 DTE from today
        next_expiries = sorted([
            (datetime.strptime(e, "%Y%m%d").date(), e)
            for e in chain.expirations
            if datetime.strptime(e, "%Y%m%d").date() > cur_exp_date
            and (datetime.strptime(e, "%Y%m%d").date() - today).days >= 14
        ])
        if not next_expiries:
            return None

        roll_exp_date, roll_exp_str = next_expiries[0]
        roll_dte = (roll_exp_date - today).days

        # Candidate strikes: same, -1, -2 from current (more OTM for puts)
        avail = sorted(chain.strikes)
        try:
            idx = next(i for i, s in enumerate(avail) if abs(s - cur_strike) < 0.01)
        except StopIteration:
            idx = None

        candidates = []
        if idx is not None:
            for offset in (0, -1, -2):
                i = idx + offset
                if 0 <= i < len(avail):
                    candidates.append(avail[i])
        if not candidates:
            candidates = [cur_strike]

        # Fetch close debit for current leg
        close_opt = Option(underlying, cur_expiry.replace("-", ""), cur_strike, "P", "SMART")
        try:
            [qclose] = await self.ibkr.ib.qualifyContractsAsync(close_opt)
            ctd = self.ibkr.ib.reqMktData(qclose, genericTickList="", snapshot=False)
            await asyncio.sleep(3)
            self.ibkr.ib.cancelMktData(qclose)
            close_debit = ctd.ask if (ctd.ask and not math.isnan(ctd.ask) and ctd.ask > 0) else None
        except Exception as exc:
            logger.warning("build_roll_proposal close quote failed: %s", exc)
            return None

        if close_debit is None:
            return None

        # Find best roll strike (first that gives positive net credit)
        roll_strike = roll_credit = new_credit = None
        for strike in candidates:
            opt = Option(underlying, roll_exp_str, strike, "P", "SMART")
            try:
                [qopt] = await self.ibkr.ib.qualifyContractsAsync(opt)
            except Exception:
                continue

            td = self.ibkr.ib.reqMktData(qopt, genericTickList="", snapshot=False)
            await asyncio.sleep(3)
            self.ibkr.ib.cancelMktData(qopt)

            if not td.bid or math.isnan(td.bid) or td.bid <= 0:
                continue

            net = td.bid - close_debit
            roll_strike = strike
            new_credit  = td.bid
            roll_credit = net
            if net > 0:
                break  # prefer the first positive-net-credit candidate

        if roll_strike is None:
            return None

        return {
            "trade_id":         trade_id,
            "underlying":       underlying,
            "current_strike":   cur_strike,
            "current_expiry":   cur_expiry,
            "roll_strike":      roll_strike,
            "roll_expiry":      roll_exp_date.isoformat(),
            "roll_dte":         roll_dte,
            "close_debit":      round(close_debit, 2),
            "new_credit":       round(new_credit, 2) if new_credit else None,
            "net_credit":       round(roll_credit, 2),
            "net_credit_total": round(roll_credit * 100, 2),
            "is_debit":         roll_credit < 0,
        }
