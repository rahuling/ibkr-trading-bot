"""
M6: Risk Engine.

All hard risk rules enforced here. This module is the circuit breaker —
it gates every new trade and every order submission.

CRITICAL DESIGN RULE: All dollar limits are computed dynamically from
live portfolio value at the time of each check. Never use hardcoded
dollar amounts. portfolio_value is fetched from IBKR on every call.

PRD reference: §5 M6 Risk Engine, §10 Risk Rules.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

async def _sum_pnl_since(db, since: datetime) -> float:
    """Sum realized P&L from trades closed on or after `since`."""
    async with db.execute(
        """SELECT COALESCE(SUM(pnl), 0) FROM trades
           WHERE status = 'closed' AND exit_date >= ? AND pnl IS NOT NULL""",
        (since.isoformat(),),
    ) as cur:
        row = await cur.fetchone()
    return float(row[0]) if row and row[0] is not None else 0.0


def _capital_from_legs(legs: list) -> float:
    """
    Estimate capital required from stored leg JSON.

    CSP:    short_strike * 100  (full cash-secured collateral)
    Spread: spread_width * 100  (max loss = collateral)
    """
    if not legs:
        return 0.0
    sell = [l for l in legs if l.get("action") == "SELL"]
    buy  = [l for l in legs if l.get("action") == "BUY"]
    if not sell:
        return 0.0
    if not buy:
        return sell[0].get("strike", 0) * 100
    return (sell[0].get("strike", 0) - buy[0].get("strike", 0)) * 100


# ---------------------------------------------------------------------------
# Risk Engine
# ---------------------------------------------------------------------------

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
        self.on_notify = None  # set by TelegramBot.wire()

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

    async def check_new_trade(self, underlying: str, capital_required: float, bucket: str) -> RiskStatus:
        """
        Gate check before generating or approving any new proposal.

        Checks (in order):
          1. System not paused
          2. PDT threshold not breached
          3. Daily / weekly / monthly loss limits not hit
          4. Underlying not at max exposure
          5. Capital available in bucket
        """
        if self._paused:
            return RiskStatus(RiskCheckResult.BLOCKED, "System is paused — use /resume")

        pdt = self.check_pdt()
        if pdt.result == RiskCheckResult.BLOCKED:
            return pdt

        loss = await self.check_loss_limits()
        if loss.result == RiskCheckResult.BLOCKED:
            return loss

        exposure = await self.check_underlying_exposure(underlying)
        if exposure.result == RiskCheckResult.BLOCKED:
            return exposure

        capacity = await self._check_bucket_capacity(underlying, capital_required, bucket)
        if capacity.result == RiskCheckResult.BLOCKED:
            return capacity

        try:
            pv = self.get_portfolio_value()
        except RuntimeError:
            pv = None

        return RiskStatus(RiskCheckResult.OK, "All risk checks passed", portfolio_value=pv)

    async def check_underlying_exposure(self, underlying: str) -> RiskStatus:
        """
        Check if adding a position would exceed max_underlying_pct exposure
        in a single underlying.
        """
        from bot.database import get_db

        try:
            pv = self.get_portfolio_value()
        except RuntimeError as exc:
            return RiskStatus(RiskCheckResult.WARNING, str(exc))

        max_exposure = pv * self.config.risk.max_underlying_pct

        async with get_db() as db:
            async with db.execute(
                "SELECT legs FROM trades WHERE underlying = ? AND status = 'open'",
                (underlying,),
            ) as cur:
                rows = await cur.fetchall()

        total = sum(_capital_from_legs(json.loads(r["legs"])) for r in rows)

        if total >= max_exposure:
            return RiskStatus(
                RiskCheckResult.BLOCKED,
                f"{underlying} exposure ${total:,.0f} at or above "
                f"{self.config.risk.max_underlying_pct * 100:.0f}% limit "
                f"(${max_exposure:,.0f})",
                portfolio_value=pv,
            )
        return RiskStatus(RiskCheckResult.OK, f"{underlying} exposure OK", portfolio_value=pv)

    async def check_loss_limits(self) -> RiskStatus:
        """
        Check daily, weekly, and monthly P&L against configured limits.

        Realizes P&L comes from the trades table; unrealized from positions table.
        Triggers auto-pause and Telegram alert if any limit is breached.
        """
        from bot.database import get_db

        try:
            pv = self.get_portfolio_value()
        except RuntimeError as exc:
            return RiskStatus(RiskCheckResult.WARNING, str(exc))

        r = self.config.risk
        daily_limit   = pv * r.daily_loss_limit_pct
        weekly_limit  = pv * r.weekly_loss_limit_pct
        monthly_limit = pv * r.monthly_loss_limit_pct

        now = datetime.now(timezone.utc)
        day_start   = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start  = day_start - timedelta(days=now.weekday())
        month_start = day_start.replace(day=1)

        async with get_db() as db:
            daily_realized   = await _sum_pnl_since(db, day_start)
            weekly_realized  = await _sum_pnl_since(db, week_start)
            monthly_realized = await _sum_pnl_since(db, month_start)

            async with db.execute(
                "SELECT COALESCE(SUM(unrealised_pnl), 0) FROM positions"
            ) as cur:
                row = await cur.fetchone()
            unrealized = float(row[0]) if row and row[0] is not None else 0.0

        # Unrealized P&L applies to the daily check only (intraday exposure)
        daily_total = daily_realized + unrealized

        def _check(label, total, limit, pause_reason):
            if total <= -abs(limit):
                self.pause(pause_reason)
                return RiskStatus(
                    RiskCheckResult.BLOCKED,
                    f"{label} loss limit hit: ${total:,.0f} loss > ${limit:,.0f} limit",
                    portfolio_value=pv,
                )
            return None

        for result in [
            _check("Daily",   daily_total,      daily_limit,   "Daily loss limit breached"),
            _check("Weekly",  weekly_realized,  weekly_limit,  "Weekly loss limit breached"),
            _check("Monthly", monthly_realized, monthly_limit, "Monthly loss limit breached"),
        ]:
            if result:
                if self.on_notify:
                    await self.on_notify(
                        f"🚨 {result.reason}\nNew trades paused. Use /resume after review."
                    )
                return result

        return RiskStatus(RiskCheckResult.OK, "Loss limits OK", portfolio_value=pv)

    def check_pdt(self) -> RiskStatus:
        """
        Check Pattern Day Trader rule.

        - Portfolio < pdt_warning_threshold ($30k): warning
        - Portfolio < pdt_stop_threshold ($25k): block same-day open/close sequences
        """
        try:
            pv = self.get_portfolio_value()
        except RuntimeError as exc:
            return RiskStatus(RiskCheckResult.WARNING, str(exc))

        if pv < self.config.risk.pdt_stop_threshold:
            return RiskStatus(
                RiskCheckResult.BLOCKED,
                f"Portfolio ${pv:,.0f} below PDT stop threshold "
                f"${self.config.risk.pdt_stop_threshold:,.0f}",
                portfolio_value=pv,
            )
        if pv < self.config.risk.pdt_warning_threshold:
            return RiskStatus(
                RiskCheckResult.WARNING,
                f"Portfolio ${pv:,.0f} approaching PDT threshold "
                f"${self.config.risk.pdt_warning_threshold:,.0f}",
                portfolio_value=pv,
            )
        return RiskStatus(RiskCheckResult.OK, "PDT OK", portfolio_value=pv)

    async def _check_bucket_capacity(
        self, underlying: str, capital_required: float, bucket: str
    ) -> RiskStatus:
        """Check per-position cap and total bucket capacity."""
        from bot.database import get_db

        try:
            budget = self.get_bucket_budget(bucket)
            per_pos_cap = self.get_per_position_cap(bucket)
        except RuntimeError as exc:
            return RiskStatus(RiskCheckResult.WARNING, str(exc))

        if capital_required > per_pos_cap:
            return RiskStatus(
                RiskCheckResult.BLOCKED,
                f"Capital required ${capital_required:,.0f} exceeds "
                f"per-position cap ${per_pos_cap:,.0f} for {bucket} bucket",
            )

        async with get_db() as db:
            async with db.execute(
                "SELECT legs FROM trades WHERE bucket = ? AND status = 'open'",
                (bucket,),
            ) as cur:
                rows = await cur.fetchall()

        used = sum(_capital_from_legs(json.loads(r["legs"])) for r in rows)
        if used + capital_required > budget:
            return RiskStatus(
                RiskCheckResult.BLOCKED,
                f"{bucket} bucket full — used ${used:,.0f} + required "
                f"${capital_required:,.0f} > budget ${budget:,.0f}",
            )

        return RiskStatus(RiskCheckResult.OK, f"{bucket} capacity OK")

    # ------------------------------------------------------------------
    # Risk dashboard
    # ------------------------------------------------------------------

    async def get_risk_summary(self) -> dict:
        """Return a dict of all risk metrics for the /risk command."""
        from bot.database import get_db

        try:
            pv = self.get_portfolio_value()
        except RuntimeError:
            pv = None

        now = datetime.now(timezone.utc)
        day_start   = now.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start  = day_start - timedelta(days=now.weekday())
        month_start = day_start.replace(day=1)

        async with get_db() as db:
            daily_realized   = await _sum_pnl_since(db, day_start)
            weekly_realized  = await _sum_pnl_since(db, week_start)
            monthly_realized = await _sum_pnl_since(db, month_start)

            async with db.execute(
                "SELECT COALESCE(SUM(unrealised_pnl), 0) FROM positions"
            ) as cur:
                row = await cur.fetchone()
            unrealized = float(row[0]) if row and row[0] is not None else 0.0

            bucket_counts = {}
            bucket_exposures = {}
            for bucket in ("Core", "Tactical", "Momentum"):
                async with db.execute(
                    "SELECT COUNT(*) FROM trades WHERE bucket = ? AND status = 'open'",
                    (bucket,),
                ) as cur:
                    r = await cur.fetchone()
                bucket_counts[bucket] = r[0] if r else 0

                async with db.execute(
                    "SELECT legs FROM trades WHERE bucket = ? AND status = 'open'",
                    (bucket,),
                ) as cur:
                    rows = await cur.fetchall()
                bucket_exposures[bucket] = sum(
                    _capital_from_legs(json.loads(row["legs"])) for row in rows
                )

        r = self.config.risk
        daily_limit   = pv * r.daily_loss_limit_pct   if pv else None
        weekly_limit  = pv * r.weekly_loss_limit_pct  if pv else None
        monthly_limit = pv * r.monthly_loss_limit_pct if pv else None

        return {
            "portfolio_value":   pv,
            "paused":            self._paused,
            "daily_pnl":         daily_realized + unrealized,
            "weekly_pnl":        weekly_realized,
            "monthly_pnl":       monthly_realized,
            "unrealized_pnl":    unrealized,
            "daily_limit":       daily_limit,
            "weekly_limit":      weekly_limit,
            "monthly_limit":     monthly_limit,
            "bucket_counts":     bucket_counts,
            "bucket_exposures":  bucket_exposures,
            "pdt_status":        self.check_pdt() if pv else None,
        }

    async def format_risk_dashboard(self) -> str:
        """Format /risk output as a plain-text Telegram message."""
        s = await self.get_risk_summary()
        r = self.config.risk

        pv = s["portfolio_value"]
        pv_str = f"${pv:,.0f}" if pv else "N/A"

        lines = [
            "📊 RISK DASHBOARD",
            "──────────────────────",
            f"Portfolio:  {pv_str}",
            f"Paused:     {'Yes ⏸' if s['paused'] else 'No'}",
            "",
            "Capital Allocation",
        ]

        bucket_pcts = {
            "Core":     r.core_bucket_pct,
            "Tactical": r.tactical_bucket_pct,
            "Momentum": r.momentum_bucket_pct,
        }
        for bucket, pct in bucket_pcts.items():
            budget = (pv * pct) if pv else None
            used   = s["bucket_exposures"].get(bucket, 0)
            count  = s["bucket_counts"].get(bucket, 0)
            budget_str = f"${budget:,.0f}" if budget else "N/A"
            lines.append(
                f"  {bucket:<9} ${used:>7,.0f} / {budget_str}  ({pct*100:.0f}%)  {count} pos"
            )

        def _pnl_line(label, pnl, limit):
            if limit is None:
                return f"  {label}: ${pnl:,.0f}"
            used_pct = abs(pnl / limit * 100) if limit else 0
            sign = "+" if pnl >= 0 else ""
            return f"  {label}: {sign}${pnl:,.0f}  /  -${abs(limit):,.0f} limit  ({used_pct:.0f}%)"

        lines += [
            "",
            "P&L",
            _pnl_line("Daily  (realized+unreal)", s["daily_pnl"],  s["daily_limit"]),
            _pnl_line("Weekly (realized)        ", s["weekly_pnl"], s["weekly_limit"]),
            _pnl_line("Monthly (realized)       ", s["monthly_pnl"], s["monthly_limit"]),
        ]

        pdt = s.get("pdt_status")
        if pdt:
            icon = {"ok": "✅", "warning": "⚠️", "blocked": "🚨"}[pdt.result.value]
            lines += ["", f"PDT: {icon} {pdt.reason}"]

        return "\n".join(lines)
