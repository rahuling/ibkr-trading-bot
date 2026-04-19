"""
M5: Position Manager.

Tracks all open positions, monitors Greeks and P&L, triggers management
rules, and tracks Wheel cycles.

Price monitoring is EVENT-DRIVEN via ib_async tick subscriptions —
not polled on a 5-minute timer. Strike-tested alerts fire in seconds,
not at the next poll cycle.

PRD reference: §5 M5 Position Manager.
"""

import logging
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
        self._subscriptions: Dict[str, object] = {}   # ticker -> ib_async Ticker

    # ------------------------------------------------------------------
    # Startup
    # ------------------------------------------------------------------

    async def subscribe_all_open_positions(self, db) -> None:
        """
        On startup: subscribe to price tick events for all open positions.

        Called after state reconciliation. For each open position in DB,
        reqMktData on the underlying and register on_price_update callback.

        TODO (Phase 4): implement.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Event-driven price monitoring
    # ------------------------------------------------------------------

    def subscribe_position(self, position, on_alert: Callable) -> None:
        """
        Subscribe to real-time price ticks for a position's underlying.

        Registers on_price_update callback that fires on every tick change.
        This handles: strike tested, LEAP stop/target, profit target alerts.

        TODO (Phase 4): implement using ib_async ticker.updateEvent.
        """
        raise NotImplementedError

    def unsubscribe_position(self, position) -> None:
        """Cancel price tick subscription when a position is closed."""
        raise NotImplementedError

    async def on_price_update(self, ticker, position) -> None:
        """
        Callback fired on every price tick for a monitored position.

        Checks:
          - For CSP/Spread: is underlying within 2% of short strike?
          - For LEAP: has underlying hit stop_price or profit_target_price?
          - For all: has P&L hit the profit close threshold?

        PRD §5 M5 — Automated Management Triggers.
        PRD §5 M5 — LEAP Stop-Loss Monitoring.

        TODO (Phase 4): implement.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Management triggers
    # ------------------------------------------------------------------

    async def check_all_positions(self, db, on_alert: Callable) -> None:
        """
        5-minute fallback poll: check all open positions for management triggers.

        Supplements the event-driven monitoring — catches anything that
        the tick subscription might have missed (e.g. during reconnect).

        TODO (Phase 4): implement.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # LEAP stop/target execution
    # ------------------------------------------------------------------

    async def trigger_leap_stop_loss(self, position, current_underlying_price: float) -> None:
        """
        Close a LEAP position because underlying hit stop_price.

        Order priority: limit at mid → bid fallback (3 min) → market (2 more min)
        Priority is getting out, not getting a good price.

        PRD §5 M5 — LEAP Stop-Loss Monitoring.

        TODO (Phase 4): implement using execution_engine.close_position.
        """
        raise NotImplementedError

    async def trigger_leap_profit_target(self, position, current_underlying_price: float) -> None:
        """Close a LEAP position because underlying hit profit_target_price."""
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Wheel cycle management
    # ------------------------------------------------------------------

    async def create_wheel_cycle(self, db, underlying: str, csp_trade_id: str) -> str:
        """
        Create a new wheel_cycle record when a CSP is entered on a Core ticker.

        Returns the new cycle_id.

        TODO (Phase 4): implement.
        """
        raise NotImplementedError

    async def handle_assignment(self, db, position, on_alert: Callable) -> None:
        """
        Called when IBKR account update shows assignment (long stock appears,
        short put disappears).

        Steps (PRD §5 M5 — Assignment Handling):
          1. Mark CSP trade: status=closed, outcome=Assigned
          2. Create stock position record
          3. Update wheel_cycle: shares_assigned=True, csp_closed_at, csp_pnl
          4. Queue CC proposal for next trading day's 9:45am scan
          5. Alert user via Telegram

        NOTE: Do not attempt to submit a Covered Call order after hours.

        TODO (Phase 4): implement.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Roll logic
    # ------------------------------------------------------------------

    async def build_roll_proposal(self, db, position_id: str) -> Optional[dict]:
        """
        Build a roll proposal for /roll [id].

        Steps (PRD §5 M5 — Roll Logic):
          1. Fetch current strike and expiry
          2. Build roll: same or 1–2 strikes OTM, next monthly expiry
          3. Compute net credit (new credit - close debit)
          4. If net credit > 0: return proposal for approval
          5. If net debit: return proposal flagged as debit roll (requires explicit approval)

        Never auto-approve a debit roll.

        TODO (Phase 4): implement.
        """
        raise NotImplementedError
