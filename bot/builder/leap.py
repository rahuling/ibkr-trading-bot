"""
M2: EOD LEAP Call trade builder.

Selects a deep ITM LEAP call (delta 0.73–0.83, DTE 300–420) as a
capital-efficient stock substitute, and sets stop-loss and profit-target
levels on the underlying price.

PRD reference: §5 M2 Trade Builder — Logic: EOD LEAP Call.
§9 Strategy 3: EOD LEAP Call (Momentum Bucket).

Stop-loss and profit-target are monitored on the UNDERLYING price,
not the LEAP mark price — LEAP bid-ask spreads are too wide for
price-based stops to work reliably on the option itself.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LEAPProposal:
    underlying: str
    underlying_price: float     # spot price at scan time
    expiry: str
    dte: int
    strike: float
    ask_price: float            # use ask (not mid) for LEAP cost estimate
    cost_total: float           # ask_price * 100
    intrinsic_value: float      # (underlying_price - strike) * 100
    extrinsic_value: float      # cost_total - intrinsic_value
    extrinsic_pct: float        # extrinsic_value / cost_total
    delta: float
    stop_price: float           # underlying_price * (1 - stop_loss_pct)
    profit_target_price: float  # underlying_price * (1 + profit_target_pct)
    realistic_risk: float       # stop_distance * delta * 100
    pct_from_day_high: float
    volume_ratio: float
    rule_tags: list
    entry_signals: dict


async def build_leap_proposal(config, ibkr, candidate) -> Optional[LEAPProposal]:
    """
    Build a LEAP call proposal for a momentum candidate.

    Steps (PRD §5 M2 Logic — EOD LEAP Call):
      1. Fetch call chain for DTE 300–420 days
      2. Select strike with delta closest to 0.78 (tolerance ±0.05)
      3. Use ask price for cost estimate (bid-ask spread too wide to rely on mid)
      4. Compute intrinsic/extrinsic values
      5. Reject if extrinsic > max_extrinsic_pct of total cost
      6. Compute stop_price and profit_target_price from underlying_price
      7. Validate: cost_total <= per_position_cap for Momentum bucket

    Returns None if no valid LEAP found or capital unavailable.

    TODO (Phase 5b): implement.
    """
    raise NotImplementedError


def format_leap_trade_card(proposal: LEAPProposal, proposal_id: str) -> str:
    """Format a LEAP proposal as a Telegram trade card."""
    return (
        f"📋 TRADE PROPOSAL #{proposal_id}\n"
        f"──────────────────────────────\n"
        f"Underlying:  {proposal.underlying} (${proposal.underlying_price:.2f})"
        f"  ↑ Near day high\n"
        f"Strategy:    EOD LEAP Call (Momentum)\n"
        f"LEAP:        {proposal.expiry} ${proposal.strike:.0f} Call ({proposal.dte} DTE)\n"
        f"Cost:        ${proposal.ask_price:.2f}  (${proposal.cost_total:,.0f} total)\n"
        f"Delta:       {proposal.delta:.2f}"
        f"  |  Intrinsic: ${proposal.intrinsic_value:,.0f}"
        f"  |  Extrinsic: ${proposal.extrinsic_value:,.0f}\n"
        f"\n"
        f"Entry at:    ${proposal.underlying_price:.2f} (underlying)\n"
        f"Stop loss:   ${proposal.stop_price:.2f}"
        f"  ({(1 - proposal.stop_price / proposal.underlying_price) * 100:.1f}% on underlying)\n"
        f"Profit tgt:  ${proposal.profit_target_price:.2f}"
        f"  (+{(proposal.profit_target_price / proposal.underlying_price - 1) * 100:.1f}% on underlying)\n"
        f"Real. risk:  ~${proposal.realistic_risk:,.0f}  (stop dist × delta × 100)\n"
        f"\n"
        f"Vol ratio:   {proposal.volume_ratio:.1f}×"
        f"  |  % from high: {proposal.pct_from_day_high:.1f}%\n"
        f"Capital req: ${proposal.cost_total:,.0f} (Momentum bucket)\n"
        f"\n"
        f"✅ /approve {proposal_id}    ❌ /reject {proposal_id}"
    )
