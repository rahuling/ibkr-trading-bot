"""
M2: Covered Call trade builder.

Builds a Covered Call proposal after a CSP has been assigned. The call
is sold OTM against the 100 shares now held in the account.

Parameters (from config.trading.covered_call):
  - DTE range:     7–21 days
  - Target delta:  0.28  (slightly OTM)
  - Delta tolerance: ±0.05
  - Profit close:  75% of max credit

PRD reference: §5 Strategy 1 — Wheel (Covered Call phase).
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class CCProposal:
    underlying: str
    underlying_price: float
    expiry: str              # "YYYY-MM-DD"
    dte: int
    strike: float
    bid_price: float
    credit_per_share: float
    credit_total: float
    delta: float
    net_cost_basis: float    # strike - csp_credit, shown as context
    rule_tags: list
    capital_required: float = 0.0  # CC is collateralised by owned shares — no additional cash locked
    ivr: Optional[float] = None


async def build_cc_proposal(
    config, ibkr, underlying: str, net_cost_basis: float
) -> Optional[CCProposal]:
    """
    Find a Covered Call to sell against 100 assigned shares.

    Steps:
      1. Fetch current stock price
      2. Get option chain — find expiries in DTE range
      3. Filter strikes: ATM to 10% OTM calls
      4. Iterate to find delta near target_delta (0.28 ± tolerance)
      5. Return CCProposal or None if market data unavailable
    """
    from ib_async import Option, Stock

    cfg = config.trading.covered_call
    today = date.today()

    try:
        stock = Stock(underlying, "SMART", "USD")
        [qs] = await ibkr.ib.qualifyContractsAsync(stock)
    except Exception as exc:
        logger.error("build_cc_proposal qualify %s: %s", underlying, exc)
        return None

    td_stock = ibkr.ib.reqMktData(qs, genericTickList="", snapshot=False)
    await asyncio.sleep(3)
    ibkr.ib.cancelMktData(qs)

    price = td_stock.last if (td_stock.last and not math.isnan(td_stock.last) and td_stock.last > 0) else None
    if price is None:
        price = td_stock.close if (td_stock.close and not math.isnan(td_stock.close) and td_stock.close > 0) else None
    if price is None:
        logger.warning("build_cc_proposal: no price for %s — market may be closed", underlying)
        return None

    chains = await ibkr.ib.reqSecDefOptParamsAsync(qs.symbol, "", qs.secType, qs.conId)
    if not chains:
        logger.info("build_cc_proposal %s: no option chain", underlying)
        return None

    chain = chains[0]

    valid_expiries = []
    for e in chain.expirations:
        try:
            exp_date = datetime.strptime(e, "%Y%m%d").date()
            dte = (exp_date - today).days
            if cfg.dte_min <= dte <= cfg.dte_max:
                valid_expiries.append((dte, e, exp_date))
        except ValueError:
            continue

    if not valid_expiries:
        logger.info("build_cc_proposal %s: no expiry in %d–%d DTE range", underlying, cfg.dte_min, cfg.dte_max)
        return None

    # Prefer expiry closest to midpoint of DTE range
    target_dte = (cfg.dte_min + cfg.dte_max) // 2
    valid_expiries.sort(key=lambda x: abs(x[0] - target_dte))

    # Candidate strikes: ATM to 10% OTM calls
    min_strike = price * 0.99
    max_strike = price * 1.10
    candidate_strikes = sorted(
        s for s in chain.strikes if min_strike <= s <= max_strike
    )

    if not candidate_strikes:
        logger.info(
            "build_cc_proposal %s: no strikes in ATM-OTM range [%.0f, %.0f]",
            underlying, min_strike, max_strike,
        )
        return None

    for dte_val, exp_str, exp_date in valid_expiries:
        for strike in candidate_strikes:
            try:
                opt = Option(underlying, exp_str, strike, "C", "SMART")
                try:
                    [qopt] = await ibkr.ib.qualifyContractsAsync(opt)
                except Exception:
                    continue

                td = ibkr.ib.reqMktData(qopt, genericTickList="", snapshot=False)
                await asyncio.sleep(3)
                ibkr.ib.cancelMktData(qopt)

                bid = td.bid if (td.bid and not math.isnan(td.bid) and td.bid > 0) else None
                if bid is None:
                    continue

                if not td.modelGreeks or not td.modelGreeks.delta:
                    continue
                delta = td.modelGreeks.delta
                if math.isnan(delta):
                    continue

                if abs(delta - cfg.target_delta) > 0.05:
                    continue

                rule_tags = ["covered_call"]
                if strike >= net_cost_basis:
                    rule_tags.append("above_cost_basis")

                logger.info(
                    "CC proposal built: %s %s %.0f C  delta=%.2f  bid=%.2f  dte=%d",
                    underlying, exp_str, strike, delta, bid, dte_val,
                )

                return CCProposal(
                    underlying=underlying,
                    underlying_price=round(float(price), 2),
                    expiry=exp_date.isoformat(),
                    dte=dte_val,
                    strike=strike,
                    bid_price=round(bid, 2),
                    credit_per_share=round(bid, 2),
                    credit_total=round(bid * 100, 2),
                    delta=round(delta, 3),
                    net_cost_basis=round(net_cost_basis, 2),
                    rule_tags=rule_tags,
                )

            except Exception as exc:
                logger.warning("build_cc_proposal %s strike %.0f: %s", underlying, strike, exc)
                continue

    logger.info("build_cc_proposal %s: no strike satisfied delta constraint", underlying)
    return None


def format_cc_trade_card(proposal: CCProposal, proposal_id: str) -> str:
    """Format a Covered Call proposal as a Telegram trade card."""
    above = "✅ above cost basis" if proposal.strike >= proposal.net_cost_basis else "⚠️ below cost basis"
    return (
        f"📋 TRADE PROPOSAL #{proposal_id}\n"
        f"──────────────────────────────\n"
        f"Underlying:  {proposal.underlying} (${proposal.underlying_price:.2f})\n"
        f"Strategy:    Covered Call (Wheel — Core bucket)\n"
        f"Contract:    {proposal.expiry} ${proposal.strike:.0f} Call ({proposal.dte} DTE)\n"
        f"Credit:      ${proposal.bid_price:.2f}/share  (${proposal.credit_total:.0f} total)\n"
        f"Delta:       {proposal.delta:.2f}\n"
        f"\n"
        f"Cost basis:  ${proposal.net_cost_basis:.2f}/share  {above}\n"
        f"\n"
        f"✅ /approve {proposal_id}    ❌ /reject {proposal_id}"
    )
