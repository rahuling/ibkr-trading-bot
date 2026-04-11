"""
M2: CSP (Cash Secured Put) trade builder.

Given a scan candidate, selects the optimal strike and constructs
a fully-specified trade proposal.

PRD reference: §5 M2 Trade Builder — Logic: Cash Secured Put.
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CSPProposal:
    underlying: str
    underlying_price: float
    expiry: str                 # e.g. "2026-05-16"
    dte: int
    strike: float
    credit_per_share: float
    credit_total: float         # credit_per_share * 100
    net_cost_basis: float       # strike - credit_per_share (breakeven if assigned)
    assignment_cost: float      # net_cost_basis * 100
    capital_required: float     # strike * 100 (full cash-secured collateral)
    annualised_return: float    # (credit_total / capital_required) * (365 / dte)
    delta: float
    ivr: float
    rule_tags: list
    entry_signals: dict


async def build_csp_proposal(config, ibkr, candidate) -> Optional[CSPProposal]:
    """
    Build a CSP proposal for a scan candidate.

    Steps (PRD §5 M2 Logic — Cash Secured Put):
      1. Fetch put chain for DTE 30–45 days
      2. Find strike closest to target_delta (default -0.27 ± tolerance)
      3. Use bid if bid-ask spread > $0.15, else use mid
      4. Compute credit, net_cost_basis, assignment_cost, capital_required, ann_return
      5. Validate: capital_required <= min(available_bucket_balance, per_position_cap)

    Returns None if no valid strike found or capital not available.

    TODO (Phase 2): implement.
    """
    raise NotImplementedError


def format_csp_trade_card(proposal: CSPProposal, proposal_id: str) -> str:
    """
    Format a CSP proposal as a Telegram trade card.

    PRD §5 M2 Trade Card Format (CSP).
    """
    from datetime import datetime
    expires_str = "See proposal"   # TODO: compute from proposal creation time + TTL

    return (
        f"📋 TRADE PROPOSAL #{proposal_id}\n"
        f"─────────────────────────\n"
        f"Underlying:  {proposal.underlying} (${proposal.underlying_price:.2f})\n"
        f"Strategy:    Cash Secured Put\n"
        f"Expiry:      {proposal.expiry} ({proposal.dte} DTE)\n"
        f"Strike:      ${proposal.strike:.0f} Put\n"
        f"Credit:      ${proposal.credit_per_share:.2f} (${proposal.credit_total:.0f} total)\n"
        f"\n"
        f"If expires worthless:  +${proposal.credit_total:.0f} profit\n"
        f"If assigned:\n"
        f"  Net cost basis:  ${proposal.net_cost_basis:.2f}/share\n"
        f"  Capital held:    ${proposal.capital_required:,.0f}\n"
        f"  → Proceeds to Covered Call leg\n"
        f"\n"
        f"Delta:       {proposal.delta:.2f}  |  IVR: {proposal.ivr:.0f}\n"
        f"Ann. return: {proposal.annualised_return * 100:.1f}%\n"
        f"Capital req: ${proposal.capital_required:,.0f}\n"
        f"Proposal expires: {expires_str}\n"
        f"\n"
        f"✅ /approve {proposal_id}    ❌ /reject {proposal_id}"
    )
