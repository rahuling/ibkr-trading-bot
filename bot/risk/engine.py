"""
M6: Risk Engine.

All hard risk rules enforced here. This module is the circuit breaker —
it gates every new trade and every order submission.

CRITICAL DESIGN RULE: All dollar limits are computed dynamically from
live portfolio value at the time of each check. Never use hardcoded
dollar amounts. portfolio_value is fetched from IBKR on every call.

PRD reference: §5 M6 Risk Engine, §10 Risk Rules.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


class RiskCheckResult(Enum):
    OK = "ok"
    BLOCKED = "blocked"
    WARNING = "warning"


@dataclass
class RiskStatus:
    result: RiskCheckResult
    reason: str
    portfolio_value: Optional[float] = None


class RiskEngine:
    """
    Enforces all capital and loss limits.

    Called by ExecutionEngine before every order submission, and by
    the scanner before generating proposals.
    """

    def __init__(self, config, ibkr):
        self.config = config
        self.ibkr = ibkr
        self._paused = False

    # ------------------------------------------------------------------
    # Pause / resume (set by circuit breakers or user /pause command)
    # ------------------------------------------------------------------

    def pause(self, reason: str) -> None:
        self._paused = True
        logger.warning("Risk Engine PAUSED: %s", reason)

    def resume(self) -> None:
        self._paused = False
        logger.info("Risk Engine RESUMED")

    @property
    def is_paused(self) -> bool:
        return self._paused

    # ------------------------------------------------------------------
    # Dynamic limit computation
    # ------------------------------------------------------------------

    def get_portfolio_value(self) -> float:
        """
        Fetch live Net Liquidation Value from IBKR.
        This is the base for all percentage-based limits.
        """
        value = self.ibkr.get_net_liquidation()
        if value is None:
            raise RuntimeError("Cannot compute risk limits — portfolio value unavailable from IBKR")
        return value

    def get_bucket_budget(self, bucket: str) -> float:
        """Return the total budget for a bucket based on current portfolio value."""
        pv = self.get_portfolio_value()
        r = self.config.risk
        mapping = {
            "Core":     r.core_bucket_pct,
            "Tactical": r.tactical_bucket_pct,
            "Momentum": r.momentum_bucket_pct,
            "Reserve":  r.reserve_pct,
        }
        if bucket not in mapping:
            raise ValueError(f"Unknown bucket: {bucket}")
        return pv * mapping[bucket]

    def get_per_position_cap(self, bucket: str) -> float:
        """
        Return the max capital for a single new position in a bucket.

        Core + Tactical: max_position_pct_of_bucket * bucket_budget
        Momentum: 50% of bucket
        """
        budget = self.get_bucket_budget(bucket)
        if bucket == "Momentum":
            return budget * 0.50
        return budget * self.config.risk.max_position_pct_of_bucket

    # ------------------------------------------------------------------
    # Pre-trade checks
    # ------------------------------------------------------------------

    def check_new_trade(self, underlying: str, capital_required: float, bucket: str) -> RiskStatus:
        """
        Gate check before generating or approving any new proposal.

        Checks (in order):
          1. System not paused
          2. Underlying not at max exposure
          3. Capital available in bucket
          4. PDT threshold not breached
          5. Daily / weekly / monthly loss limits not hit

        TODO (Phase 4): implement all checks with live data.
        """
        if self._paused:
            return RiskStatus(RiskCheckResult.BLOCKED, "System is paused — use /resume")

        raise NotImplementedError

    def check_underlying_exposure(self, underlying: str) -> RiskStatus:
        """
        Check if adding a position would exceed 40% portfolio exposure
        in a single underlying.

        TODO (Phase 4): sum all open position capital for this underlying.
        """
        raise NotImplementedError

    def check_loss_limits(self) -> RiskStatus:
        """
        Check daily, weekly, and monthly P&L against configured limits.

        Fetches realised + unrealised P&L from DB and IBKR.
        Triggers auto-pause and Telegram alert if any limit is breached.

        TODO (Phase 4): implement.
        """
        raise NotImplementedError

    def check_pdt(self) -> RiskStatus:
        """
        Check Pattern Day Trader rule.

        - Portfolio < $30k: warning
        - Portfolio < $25k: block same-day open/close sequences

        TODO (Phase 4): implement.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Risk dashboard
    # ------------------------------------------------------------------

    def get_risk_summary(self) -> dict:
        """
        Return a dict of risk metrics for the /risk command.

        Includes: portfolio value, bucket allocations, P&L by period,
        limit status, open exposure, portfolio Greeks.

        TODO (Phase 4): implement.
        """
        raise NotImplementedError

    def format_risk_dashboard(self) -> str:
        """Format the /risk output as a Telegram message (PRD §5 M6 /risk Output)."""
        raise NotImplementedError
