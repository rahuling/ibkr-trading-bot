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
import logging
import uuid
from datetime import datetime, time
from typing import Optional

import pytz

from ib_async import LimitOrder, MarketOrder

logger = logging.getLogger(__name__)

ET = pytz.timezone("America/New_York")

# Tick size depends on option price
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

    # ------------------------------------------------------------------
    # Order blackout check
    # ------------------------------------------------------------------

    def is_in_blackout(self) -> bool:
        """
        Return True if current time is in an order blackout window.

        Blackout windows (ET):
          - 9:30–9:45am (opening volatility)
          - 3:55–4:00pm (closing risk)
        """
        now = datetime.now(ET).time()
        open_blackout_end = time(9, 45)
        close_blackout_start = time(15, 55)
        return now < open_blackout_end or now >= close_blackout_start

    # ------------------------------------------------------------------
    # Order submission
    # ------------------------------------------------------------------

    async def submit_order(self, db, contract, price: float, quantity: int, proposal_id: str) -> str:
        """
        Submit a limit order with idempotent client_order_id.

        Flow:
          1. Generate UUID client_order_id
          2. Write to orders table with status 'pending_submit'  ← BEFORE API call
          3. Submit limit order via ib_async
          4. Update orders table with ibkr_order_id, status 'submitted'
          5. Start fill monitoring

        Returns client_order_id.

        TODO (Phase 3): implement.
        """
        if self.is_in_blackout():
            raise RuntimeError("Order blocked — currently in blackout window")

        client_order_id = str(uuid.uuid4())

        # Step 2: write BEFORE submitting
        # await db.execute(
        #     "INSERT INTO orders (client_order_id, proposal_id, status, expected_price, ...) VALUES ...",
        #     ...
        # )

        # Step 3: submit
        order = LimitOrder("BUY", quantity, price)
        # trade = await self.ibkr.ib.placeOrderAsync(contract, order)

        raise NotImplementedError

    async def reprice_and_retry(self, db, client_order_id: str, original_price: float) -> None:
        """
        After reprice_wait_minutes without a fill:
          1. Cancel the current order
          2. Reprice 1 tick toward ask
          3. Resubmit once

        Tick size: $0.05 for options < $3, $0.10 for options >= $3.

        If still unfilled after reprice_retry_wait_minutes: cancel and notify user.

        TODO (Phase 3): implement.
        """
        raise NotImplementedError

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
          "bid"    — sell at bid (used for stop-loss urgency)
          "market" — emergency only, explicit Telegram warning sent first

        TODO (Phase 3): implement.
        """
        if order_type == "market":
            logger.warning("MARKET ORDER for emergency close: %s (%s)", position, reason)
            # send Telegram warning before submitting

        raise NotImplementedError

    # ------------------------------------------------------------------
    # Orphan recovery (called on startup reconciliation)
    # ------------------------------------------------------------------

    async def recover_orphaned_orders(self, db) -> None:
        """
        Find all 'pending_submit' orders in DB and check whether IBKR
        actually received them. Called on every startup.

        For each orphaned order:
          - If IBKR has it: update DB to 'submitted' or 'filled'
          - If IBKR has no record: mark as 'cancelled' in DB, send alert

        PRD §13 Failure Modes — Crash Mid-Order.

        TODO (Phase 3): implement.
        """
        raise NotImplementedError
