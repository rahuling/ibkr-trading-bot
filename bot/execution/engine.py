"""
M4: Execution Engine.

Submits, manages, and tracks orders through IBKR via ib_async.

Key design rules:
  - Always limit orders (market orders only for emergency closes)
  - client_order_id (UUID) is written to DB BEFORE order is submitted
    — enables idempotent recovery on crash restart
  - Spreads submitted as BAG/combo contracts (never individual legs)
  - Reprice logic: 1 tick toward ask after 5 min, cancel after 3 more

PRD reference: §5 M4 Execution Engine.
"""

import asyncio
import json
import logging
import math
import uuid
from datetime import datetime, time, timezone
from typing import Optional

import pytz
from ib_async import LimitOrder, MarketOrder, Option

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")


def get_tick_size(option_price: float) -> float:
    return 0.05 if option_price < 3.00 else 0.10


class ExecutionEngine:
    """
    Handles the full order lifecycle from proposal approval to fill confirmation.
    """

    def __init__(self, config, ibkr, risk_engine):
        self.config = config
        self.ibkr = ibkr
        self.risk = risk_engine
        self.on_notify = None          # set by TelegramBot.wire() after construction
        self._position_manager = None  # set via set_position_manager()

    def set_position_manager(self, pm) -> None:
        self._position_manager = pm

    # ------------------------------------------------------------------
    # Order blackout check
    # ------------------------------------------------------------------

    def is_in_blackout(self) -> bool:
        """
        Return True if current time is in an order blackout window.

        Blackout windows (ET):
          - Market open: 9:30 to 9:30 + order_blackout_open_mins  (default 9:30–9:45)
          - Market close: 16:00 - order_blackout_close_mins to 16:00  (default 3:55–4:00)

        Pre-market / after-hours are NOT flagged as blackout — the scheduler
        ensures jobs only fire during market hours. Weekends always blocked.
        """
        now_dt = datetime.now(ET)
        if now_dt.weekday() >= 5:  # Saturday=5, Sunday=6
            return True
        now = now_dt.time()
        open_blackout_start = time(9, 30)
        open_end_total_min = 9 * 60 + 30 + self.config.execution.order_blackout_open_mins
        open_blackout_end = time(open_end_total_min // 60, open_end_total_min % 60)
        close_start_total_min = 16 * 60 - self.config.execution.order_blackout_close_mins
        close_blackout_start = time(close_start_total_min // 60, close_start_total_min % 60)
        return (open_blackout_start <= now < open_blackout_end) or now >= close_blackout_start

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def submit_order(self, db, contract, price: float, quantity: int, proposal_id: str, action: str = "SELL") -> str:
        """
        Submit a limit order with idempotent client_order_id.

        Flow:
          1. Generate UUID client_order_id
          2. Write to orders table with status 'pending_submit'  ← BEFORE API call
          3. Submit limit order via ib_async (action defaults to SELL for premium-selling)
          4. Update orders table with ibkr_order_id, status 'submitted'
          5. Start fill monitoring as a background task

        Returns client_order_id.
        """
        if self.is_in_blackout():
            raise RuntimeError("Order blocked — currently in blackout window")

        client_order_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # Write to DB BEFORE the API call — guarantees idempotent recovery on crash.
        await db.execute(
            """INSERT INTO orders
               (client_order_id, proposal_id, status, expected_price, created_at, submitted_at)
               VALUES (?, ?, 'pending_submit', ?, ?, ?)""",
            (client_order_id, proposal_id, price, now.isoformat(), now.isoformat()),
        )
        await db.commit()

        order = LimitOrder(action, quantity, price)
        order.orderRef = client_order_id  # links the IBKR order back to our UUID
        order.tif = "DAY"

        try:
            ib_trade = self.ibkr.ib.placeOrder(contract, order)
        except Exception as exc:
            await db.execute(
                "UPDATE orders SET status = 'cancelled' WHERE client_order_id = ?",
                (client_order_id,),
            )
            await db.commit()
            raise RuntimeError(f"placeOrder failed: {exc}") from exc

        ibkr_order_id = ib_trade.order.orderId
        await db.execute(
            "UPDATE orders SET ibkr_order_id = ?, status = 'submitted' WHERE client_order_id = ?",
            (ibkr_order_id, client_order_id),
        )
        await db.commit()

        logger.info(
            "Order submitted: ref=%s ibkr_id=%s price=%.2f qty=%d proposal=%s",
            client_order_id, ibkr_order_id, price, quantity, proposal_id,
        )

        # Fill monitor runs as a fire-and-forget task so the caller returns immediately.
        asyncio.create_task(
            self._monitor_fill(ib_trade, client_order_id, contract, price, quantity, proposal_id, action)
        )

        return client_order_id

    async def _monitor_fill(
        self,
        ib_trade,
        client_order_id: str,
        contract,
        price: float,
        quantity: int,
        proposal_id: str,
        action: str = "SELL",
    ) -> None:
        """Poll for fill; trigger reprice_and_retry if still open after reprice_wait_minutes."""
        from bot.database import get_db

        reprice_wait_secs = self.config.execution.reprice_wait_minutes * 60

        elapsed = 0
        while elapsed < reprice_wait_secs:
            await asyncio.sleep(10)
            elapsed += 10
            if ib_trade.isDone():
                break

        async with get_db() as db:
            if ib_trade.isDone():
                if ib_trade.orderStatus.status == "Filled":
                    await self._record_fill(db, client_order_id, ib_trade.orderStatus.avgFillPrice, proposal_id)
                else:
                    await db.execute(
                        "UPDATE orders SET status = 'cancelled' WHERE client_order_id = ?",
                        (client_order_id,),
                    )
                    await db.commit()
                    logger.info("Order %s done with status %s", client_order_id, ib_trade.orderStatus.status)
            else:
                await self.reprice_and_retry(
                    db, client_order_id, price, ib_trade, contract, quantity, proposal_id, action
                )

    async def _record_fill(self, db, client_order_id: str, fill_price: float, proposal_id: str) -> None:
        """Record a completed fill: create trade row, update order row, notify user."""
        filled_at = datetime.now(timezone.utc)

        async with db.execute(
            "SELECT expected_price FROM orders WHERE client_order_id = ?", (client_order_id,)
        ) as cur:
            order_row = await cur.fetchone()
        expected_price = order_row["expected_price"] if order_row else fill_price
        slippage = fill_price - expected_price

        async with db.execute(
            "SELECT underlying, strategy, trade_card_json FROM proposals WHERE proposal_id = ?",
            (proposal_id,),
        ) as cur:
            prop_row = await cur.fetchone()

        if not prop_row:
            logger.error("_record_fill: proposal %s not found in DB", proposal_id)
            return

        underlying = prop_row["underlying"]
        strategy = prop_row["strategy"]
        proposal_data = json.loads(prop_row["trade_card_json"])

        if strategy == "CSP":
            legs = json.dumps([{
                "strike": proposal_data["strike"],
                "expiry": proposal_data["expiry"],
                "right": "P",
                "qty": 1,
                "action": "SELL",
            }])
            bucket = "Core"
            entry_delta = proposal_data.get("delta")
        elif strategy == "CoveredCall":
            legs = json.dumps([{
                "strike": proposal_data["strike"],
                "expiry": proposal_data["expiry"],
                "right": "C",
                "qty": 1,
                "action": "SELL",
            }])
            bucket = "Core"
            entry_delta = proposal_data.get("delta")
        elif strategy == "LEAPCall":
            legs = json.dumps([{
                "strike": proposal_data["strike"],
                "expiry": proposal_data["expiry"],
                "right": "C",
                "qty": 1,
                "action": "BUY",
            }])
            bucket = "Momentum"
            entry_delta = proposal_data.get("delta")
        else:  # BullPutSpread
            legs = json.dumps([
                {
                    "strike": proposal_data["short_strike"],
                    "expiry": proposal_data["expiry"],
                    "right": "P",
                    "qty": 1,
                    "action": "SELL",
                    "con_id": proposal_data["short_put_con_id"],
                },
                {
                    "strike": proposal_data["long_strike"],
                    "expiry": proposal_data["expiry"],
                    "right": "P",
                    "qty": 1,
                    "action": "BUY",
                    "con_id": proposal_data["long_put_con_id"],
                },
            ])
            bucket = "Tactical"
            entry_delta = proposal_data.get("short_delta")

        stop_price          = proposal_data.get("stop_price")          if strategy == "LEAPCall" else None
        profit_target_price = proposal_data.get("profit_target_price") if strategy == "LEAPCall" else None

        entry_config = json.dumps({
            "daily_loss_limit_pct": self.config.risk.daily_loss_limit_pct,
            "max_position_pct_of_bucket": self.config.risk.max_position_pct_of_bucket,
            "automation_level": self.config.automation.level,
        })

        trade_id = str(uuid.uuid4())
        await db.execute(
            """INSERT INTO trades
               (trade_id, underlying, strategy, bucket, legs,
                entry_date, entry_credit, entry_ivr, entry_delta, entry_dte,
                entry_underlying_price, entry_config, rule_tags, entry_signals,
                stop_price, profit_target_price, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
            (
                trade_id, underlying, strategy, bucket, legs,
                filled_at.isoformat(),
                fill_price,
                proposal_data.get("ivr"),
                entry_delta,
                proposal_data.get("dte"),
                proposal_data.get("underlying_price"),
                entry_config,
                json.dumps(proposal_data.get("rule_tags", [])),
                json.dumps(proposal_data.get("entry_signals", {})),
                stop_price,
                profit_target_price,
            ),
        )
        await db.execute(
            """UPDATE orders
               SET status = 'filled', fill_price = ?, slippage = ?, filled_at = ?, trade_id = ?
               WHERE client_order_id = ?""",
            (fill_price, slippage, filled_at.isoformat(), trade_id, client_order_id),
        )
        await db.commit()

        logger.info(
            "Fill recorded: trade=%s %s %s fill=%.2f slippage=%+.2f",
            trade_id, underlying, strategy, fill_price, slippage,
        )

        if strategy == "CSP" and self._position_manager:
            await self._position_manager.create_wheel_cycle(db, underlying, trade_id)
        elif strategy == "CoveredCall" and self._position_manager:
            await self._position_manager.link_cc_to_wheel_cycle(db, underlying, trade_id)

        # M5: subscribe immediately so event-driven monitoring starts now,
        # not at the next 5-minute check_all_positions poll.
        if self._position_manager:
            position_for_monitor = {
                "trade_id": trade_id,
                "underlying": underlying,
                "strategy": strategy,
                "legs": legs,
                "entry_credit": fill_price,
                "stop_price": stop_price,
                "profit_target_price": profit_target_price,
            }
            asyncio.create_task(self._position_manager.subscribe_position(position_for_monitor))

        if self.on_notify:
            await self.on_notify(
                f"✅ FILLED: {underlying} {strategy}\n"
                f"Fill: ${fill_price:.2f}  |  Expected: ${expected_price:.2f}  |  "
                f"Slippage: ${slippage:+.2f}"
            )

    # ------------------------------------------------------------------
    # Reprice
    # ------------------------------------------------------------------

    async def reprice_and_retry(
        self,
        db,
        client_order_id: str,
        original_price: float,
        ib_trade=None,
        contract=None,
        quantity: int = 1,
        proposal_id: str = None,
        action: str = "SELL",
    ) -> None:
        """
        After reprice_wait_minutes without a fill:
          1. Cancel the current order
          2. Reprice 1 tick toward ask
          3. Resubmit once

        Tick size: $0.05 for options < $3, $0.10 for options >= $3.

        If still unfilled after reprice_retry_wait_minutes: cancel and notify user.
        """
        # Cancel the live order and wait for IBKR to confirm.
        if ib_trade and not ib_trade.isDone():
            self.ibkr.ib.cancelOrder(ib_trade.order)
            for _ in range(30):
                if ib_trade.isDone():
                    break
                await asyncio.sleep(1)

        tick = get_tick_size(original_price)
        new_price = round(original_price - tick, 2)  # lower ask to attract fill

        logger.info(
            "Repricing %s: %.2f → %.2f (tick=%.2f)",
            client_order_id, original_price, new_price, tick,
        )

        await db.execute(
            "UPDATE orders SET status = 'repriced' WHERE client_order_id = ?",
            (client_order_id,),
        )
        await db.commit()

        new_client_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        await db.execute(
            """INSERT INTO orders
               (client_order_id, proposal_id, status, expected_price, created_at, submitted_at)
               VALUES (?, ?, 'pending_submit', ?, ?, ?)""",
            (new_client_id, proposal_id, new_price, now.isoformat(), now.isoformat()),
        )
        await db.commit()

        new_order = LimitOrder(action, quantity, new_price)
        new_order.orderRef = new_client_id
        new_order.tif = "DAY"

        try:
            new_ib_trade = self.ibkr.ib.placeOrder(contract, new_order)
        except Exception as exc:
            logger.error("reprice placeOrder failed: %s", exc)
            await db.execute(
                "UPDATE orders SET status = 'cancelled' WHERE client_order_id = ?",
                (new_client_id,),
            )
            await db.commit()
            if self.on_notify:
                await self.on_notify(
                    f"⚠️ Reprice order submission failed: {exc}\n"
                    f"Original ref: {client_order_id[:8]}..."
                )
            return

        await db.execute(
            "UPDATE orders SET ibkr_order_id = ?, status = 'submitted' WHERE client_order_id = ?",
            (new_ib_trade.order.orderId, new_client_id),
        )
        await db.commit()

        logger.info("Repriced order submitted: %s at %.2f", new_client_id, new_price)

        retry_wait_secs = self.config.execution.reprice_retry_wait_minutes * 60
        elapsed = 0
        while elapsed < retry_wait_secs:
            await asyncio.sleep(10)
            elapsed += 10
            if new_ib_trade.isDone():
                break

        if new_ib_trade.isDone() and new_ib_trade.orderStatus.status == "Filled":
            await self._record_fill(db, new_client_id, new_ib_trade.orderStatus.avgFillPrice, proposal_id)
        else:
            if not new_ib_trade.isDone():
                self.ibkr.ib.cancelOrder(new_ib_trade.order)
            await db.execute(
                "UPDATE orders SET status = 'cancelled' WHERE client_order_id = ?",
                (new_client_id,),
            )
            await db.commit()
            logger.info("Order still unfilled after reprice — cancelled: %s", new_client_id)
            if self.on_notify:
                await self.on_notify(
                    f"⚠️ Order unfilled after reprice — cancelled.\n"
                    f"Ref: {client_order_id[:8]}...\n"
                    f"Run /scan for fresh proposals."
                )

    # ------------------------------------------------------------------
    # Close position
    # ------------------------------------------------------------------

    async def close_position(
        self,
        position,
        order_type: str = "limit",
        reason: str = "manual",
    ) -> None:
        """
        Close an open position.

        order_type:
          "limit"  — mid price, reprice if needed (default)
          "bid"    — 1 tick above mid (urgent, stop-loss)
          "market" — emergency only, explicit Telegram warning sent first
        """
        underlying = position["underlying"]
        strategy = position["strategy"]
        legs = json.loads(position["legs"]) if isinstance(position["legs"], str) else position["legs"]

        if order_type == "market":
            logger.warning(
                "MARKET ORDER for emergency close: %s %s reason=%s",
                underlying, strategy, reason,
            )
            if self.on_notify:
                await self.on_notify(
                    f"⚠️ EMERGENCY MARKET CLOSE: {underlying} {strategy}\nReason: {reason}"
                )

        contract = await self._reconstruct_contract(strategy, underlying, legs)

        # LEAP (BUY to open) closes with SELL; all premium-selling strategies close with BUY.
        close_action = "SELL" if strategy == "LEAPCall" else "BUY"

        if order_type == "market":
            order = MarketOrder(close_action, 1)
            order.tif = "DAY"
        else:
            mid = await self._get_current_mid(contract)
            if mid is None:
                raise RuntimeError(
                    f"Cannot get market data for {underlying} {strategy} — try again"
                )

            if order_type == "bid":
                tick = get_tick_size(mid)
                price = round(mid + tick, 2)
            else:
                price = round(mid, 2)

            order = LimitOrder(close_action, 1, price)
            order.tif = "DAY"

        ib_trade = self.ibkr.ib.placeOrder(contract, order)
        logger.info(
            "Close order submitted: %s %s type=%s reason=%s", underlying, strategy, order_type, reason
        )

        asyncio.create_task(self._monitor_close_fill(ib_trade, position, reason))

    async def _reconstruct_contract(self, strategy: str, underlying: str, legs: list):
        """Rebuild an IBKR contract from stored leg data (for close orders)."""
        if strategy in ("CSP", "CoveredCall", "LEAPCall"):
            leg = legs[0]
            expiry = leg["expiry"].replace("-", "")
            contract = Option(underlying, expiry, leg["strike"], leg["right"], "SMART")
            [qualified] = await self.ibkr.ib.qualifyContractsAsync(contract)
            return qualified

        if strategy == "BullPutSpread":
            from bot.builder.spread import build_bag_contract
            short_leg = next(l for l in legs if l["action"] == "SELL")
            long_leg = next(l for l in legs if l["action"] == "BUY")
            return build_bag_contract(
                self.ibkr, underlying, short_leg["con_id"], long_leg["con_id"]
            )

        raise ValueError(f"_reconstruct_contract: unsupported strategy '{strategy}'")

    async def _get_current_mid(self, contract) -> Optional[float]:
        """Request a brief market data stream and return the bid-ask mid."""
        td = self.ibkr.ib.reqMktData(contract, genericTickList="", snapshot=False)
        await asyncio.sleep(3)
        self.ibkr.ib.cancelMktData(contract)

        bid = td.bid if td.bid and not math.isnan(td.bid) and td.bid > 0 else None
        ask = td.ask if td.ask and not math.isnan(td.ask) and td.ask > 0 else None

        if bid is None or ask is None:
            return None
        return (bid + ask) / 2

    async def _monitor_close_fill(self, ib_trade, position, reason: str) -> None:
        """Wait for a close order to fill, then update DB and notify."""
        from bot.database import get_db

        wait_secs = self.config.execution.reprice_wait_minutes * 60
        elapsed = 0
        while elapsed < wait_secs:
            await asyncio.sleep(10)
            elapsed += 10
            if ib_trade.isDone():
                break

        underlying = position.get("underlying", "?")
        strategy = position.get("strategy", "?")
        trade_id = position.get("trade_id")
        entry_credit = position.get("entry_credit")

        if ib_trade.isDone() and ib_trade.orderStatus.status == "Filled":
            fill_price = ib_trade.orderStatus.avgFillPrice
            strategy = position.get("strategy", "")
            if entry_credit:
                # LEAP: bought at entry_credit, sold at fill → profit = fill - cost
                # Premium-selling: sold at entry_credit, bought back at fill → profit = credit - fill
                if strategy == "LEAPCall":
                    pnl = round((fill_price - entry_credit) * 100, 2)
                else:
                    pnl = round((entry_credit - fill_price) * 100, 2)
            else:
                pnl = None
            outcome = ("Win" if pnl > 0 else ("Loss" if pnl < 0 else "BreakEven")) if pnl is not None else None

            async with get_db() as db:
                await db.execute(
                    """UPDATE trades
                       SET status = 'closed', exit_date = ?, exit_price = ?,
                           exit_reason = ?, pnl = ?, outcome = ?
                       WHERE trade_id = ?""",
                    (datetime.now(timezone.utc).isoformat(), fill_price, reason, pnl, outcome, trade_id),
                )
                await db.execute("DELETE FROM positions WHERE trade_id = ?", (trade_id,))
                await db.commit()

            logger.info(
                "Close fill recorded: trade=%s fill=%.2f pnl=%s reason=%s",
                trade_id, fill_price, pnl, reason,
            )
            if self.on_notify:
                pnl_str = f"  |  P&L: ${pnl:+.0f}" if pnl is not None else ""
                await self.on_notify(
                    f"✅ CLOSED: {underlying} {strategy}\n"
                    f"Fill: ${fill_price:.2f}  |  Reason: {reason}{pnl_str}"
                )
        else:
            if not ib_trade.isDone():
                self.ibkr.ib.cancelOrder(ib_trade.order)
            logger.warning("Close order unfilled for trade=%s — cancelled", trade_id)
            if self.on_notify:
                await self.on_notify(
                    f"⚠️ Close order unfilled for {underlying} {strategy} — cancelled.\n"
                    f"Use /close to retry or close manually in TWS."
                )

    # ------------------------------------------------------------------
    # Orphan recovery (called on startup reconciliation)
    # ------------------------------------------------------------------

    async def recover_orphaned_orders(self, db) -> None:
        """
        On startup: recover orders that were in-flight during a crash.

        pending_submit — crash before/during placeOrder:
          If found in IBKR open orders → update to 'submitted'.
          If not found → mark 'cancelled'.

        submitted — crash after submit but before _record_fill:
          If still open in IBKR → leave as 'submitted' (fill monitor will handle it).
          If found in today's execution reports → synthesize _record_fill.
          Otherwise → mark 'cancelled'.

        PRD §13 Failure Modes — Crash Mid-Order.
        """
        async with db.execute(
            "SELECT client_order_id, ibkr_order_id FROM orders WHERE status = 'pending_submit'"
        ) as cur:
            pending_rows = await cur.fetchall()

        async with db.execute(
            "SELECT client_order_id, ibkr_order_id, proposal_id FROM orders WHERE status = 'submitted'"
        ) as cur:
            submitted_rows = await cur.fetchall()

        if not pending_rows and not submitted_rows:
            logger.info("recover_orphaned_orders: nothing to recover")
            return

        logger.info(
            "recover_orphaned_orders: %d pending_submit, %d submitted",
            len(pending_rows), len(submitted_rows),
        )

        open_trades = await self.ibkr.ib.reqAllOpenOrdersAsync()
        ibkr_by_ref = {t.order.orderRef: t for t in open_trades if t.order.orderRef}
        ibkr_by_id  = {t.order.orderId: t for t in open_trades}

        for row in pending_rows:
            client_order_id = row["client_order_id"]
            ibkr_order_id   = row["ibkr_order_id"]
            found = (client_order_id in ibkr_by_ref) or (
                ibkr_order_id is not None and ibkr_order_id in ibkr_by_id
            )
            if found:
                await db.execute(
                    "UPDATE orders SET status = 'submitted' WHERE client_order_id = ?",
                    (client_order_id,),
                )
                logger.info("Recovered pending_submit %s — found in IBKR", client_order_id)
            else:
                await db.execute(
                    "UPDATE orders SET status = 'cancelled' WHERE client_order_id = ?",
                    (client_order_id,),
                )
                logger.warning("pending_submit %s not in IBKR — cancelled", client_order_id)
                if self.on_notify:
                    await self.on_notify(
                        f"⚠️ Orphaned order {client_order_id[:8]}... not found in IBKR — marked cancelled.\n"
                        "Verify open positions with /positions."
                    )

        if submitted_rows:
            fills = await self.ibkr.ib.reqExecutionsAsync()
            fill_by_ref = {
                getattr(f.execution, "orderRef", None): f
                for f in fills
                if getattr(f.execution, "orderRef", None)
            }

            for row in submitted_rows:
                client_order_id = row["client_order_id"]
                ibkr_order_id   = row["ibkr_order_id"]
                proposal_id     = row["proposal_id"]

                still_open = (client_order_id in ibkr_by_ref) or (
                    ibkr_order_id is not None and ibkr_order_id in ibkr_by_id
                )
                if still_open:
                    logger.info("Submitted order %s still open in IBKR — no action needed", client_order_id)
                    continue

                fill = fill_by_ref.get(client_order_id)
                if fill:
                    fill_price = fill.execution.price
                    logger.info(
                        "Recovering submitted→filled crash: %s at %.2f",
                        client_order_id, fill_price,
                    )
                    await self._record_fill(db, client_order_id, fill_price, proposal_id)
                else:
                    await db.execute(
                        "UPDATE orders SET status = 'cancelled' WHERE client_order_id = ?",
                        (client_order_id,),
                    )
                    logger.warning("Submitted order %s not in IBKR or fills — cancelled", client_order_id)
                    if self.on_notify:
                        await self.on_notify(
                            f"⚠️ Submitted order {client_order_id[:8]}... not found in IBKR fills — marked cancelled."
                        )

        await db.commit()
