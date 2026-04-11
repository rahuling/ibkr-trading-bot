"""
M2: Bull Put Spread trade builder.

Constructs a fully-specified spread proposal and the IBKR BAG/combo
contract required for atomic two-leg submission.

PRD reference: §5 M2 Trade Builder — Logic: Bull Put Spread.

IMPORTANT: Spread legs are submitted as a single BAG (combo) order —
never as two separate orders. Separate leg submission creates leg risk.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from ib_async import Contract, ComboLeg

logger = logging.getLogger(__name__)


@dataclass
class SpreadProposal:
    underlying: str
    underlying_price: float
    expiry: str
    dte: int
    short_strike: float
    long_strike: float
    spread_width: float         # short_strike - long_strike
    credit_per_share: float
    credit_total: float         # credit_per_share * 100
    max_loss: float             # (spread_width - credit_per_share) * 100
    max_return_pct: float       # credit_per_share / (spread_width - credit_per_share)
    credit_to_width_ratio: float
    breakeven: float            # short_strike - credit_per_share
    short_delta: float
    ivr: float
    rule_tags: list
    entry_signals: dict


async def build_spread_proposal(config, ibkr, candidate) -> Optional[SpreadProposal]:
    """
    Build a Bull Put Spread proposal.

    Steps (PRD §5 M2 Logic — Bull Put Spread):
      1. Fetch put chain for DTE 7–21 days
      2. Short leg: closest strike to target_delta (default -0.30)
      3. Long leg: spread_width points below short strike
      4. Compute credit, max_loss, max_return, credit_to_width_ratio
      5. Validate: max_loss <= max_spread_loss; credit_to_width >= 0.25

    Returns None if no valid setup or credit-to-width ratio too low.

    TODO (Phase 2): implement.
    """
    raise NotImplementedError


def build_bag_contract(ibkr, underlying: str, short_put_con_id: int, long_put_con_id: int) -> Contract:
    """
    Build an IBKR BAG (combo) contract for atomic spread submission.

    Both legs are submitted in a single order — prevents leg risk where
    one leg fills and the other doesn't.

    PRD §5 M4 Execution Engine — Spread (BAG/Combo) Orders.
    """
    combo = Contract()
    combo.symbol = underlying
    combo.secType = "BAG"
    combo.currency = "USD"
    combo.exchange = "SMART"
    combo.comboLegs = [
        ComboLeg(conId=short_put_con_id, ratio=1, action="SELL", exchange="SMART"),
        ComboLeg(conId=long_put_con_id,  ratio=1, action="BUY",  exchange="SMART"),
    ]
    return combo


def format_spread_trade_card(proposal: SpreadProposal, proposal_id: str) -> str:
    """Format a spread proposal as a Telegram trade card."""
    return (
        f"📋 TRADE PROPOSAL #{proposal_id}\n"
        f"─────────────────────────\n"
        f"Underlying:  {proposal.underlying} (${proposal.underlying_price:.2f})\n"
        f"Strategy:    Bull Put Spread\n"
        f"Expiry:      {proposal.expiry} ({proposal.dte} DTE)\n"
        f"Short:  ${proposal.short_strike:.0f} Put  |  Long: ${proposal.long_strike:.0f} Put\n"
        f"Width:  ${proposal.spread_width:.2f}\n"
        f"\n"
        f"Credit:       ${proposal.credit_per_share:.2f} (${proposal.credit_total:.0f} total)\n"
        f"Max loss:     ${proposal.max_loss:.0f}\n"
        f"Max return:   {proposal.max_return_pct * 100:.1f}%\n"
        f"Credit/width: {proposal.credit_to_width_ratio * 100:.0f}%"
        f"{'  ✅' if proposal.credit_to_width_ratio >= 0.25 else '  ⚠️ below 25%'}\n"
        f"Breakeven:    ${proposal.breakeven:.2f}\n"
        f"Short delta:  {proposal.short_delta:.2f}  |  IVR: {proposal.ivr:.0f}\n"
        f"\n"
        f"✅ /approve {proposal_id}    ❌ /reject {proposal_id}"
    )
