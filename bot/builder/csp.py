"""
M2: CSP (Cash Secured Put) trade builder.

Given a scan candidate, selects the optimal strike and constructs
a fully-specified trade proposal.

PRD reference: §5 M2 Trade Builder — Logic: Cash Secured Put.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
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
      1. Fetch option chain definition (valid expiries + strikes)
      2. Select expiry closest to midpoint of DTE range (30–45 days)
      3. Filter candidate strikes: 0.70x–1.01x of current price
      4. Request market data for up to 10 candidate puts simultaneously
      5. Pick the strike with delta closest to target_delta (default -0.27)
      6. Use bid if bid-ask spread > $0.15, else use mid
      7. Compute credit, net_cost_basis, capital_required, ann_return
      8. Validate capital_required against per-position cap
    """
    from ib_async import Option, Stock

    cfg = config.trading.csp
    ticker = candidate.ticker
    current_price = candidate.current_price

    try:
        # 1. Qualify underlying and get option chain definition
        stock = Stock(ticker, "SMART", "USD")
        [qualified_stock] = await ibkr.ib.qualifyContractsAsync(stock)

        chains = await ibkr.ib.reqSecDefOptParamsAsync(
            qualified_stock.symbol, "", qualified_stock.secType, qualified_stock.conId
        )
        if not chains:
            logger.info("No option chain returned for %s", ticker)
            return None

        # Use the first chain — expirations/strikes are consistent across exchanges
        chain = chains[0]

        # 2. Find expiry in DTE range, pick closest to midpoint
        today = date.today()
        target_dte = (cfg.dte_min + cfg.dte_max) // 2
        valid_expiries = []
        for exp_str in sorted(chain.expirations):
            exp_date = datetime.strptime(exp_str, "%Y%m%d").date()
            dte = (exp_date - today).days
            if cfg.dte_min <= dte <= cfg.dte_max:
                valid_expiries.append((dte, exp_str, exp_date))

        if not valid_expiries:
            logger.info(
                "No expiry in DTE range %d–%d for %s",
                cfg.dte_min, cfg.dte_max, ticker,
            )
            return None

        dte, expiry_str, expiry_date = min(valid_expiries, key=lambda x: abs(x[0] - target_dte))

        # 3. Filter candidate strikes to plausible range for ~0.27 delta puts
        # 0.70x–1.01x covers most realistic delta targets across low/high IV regimes
        min_strike = current_price * 0.70
        max_strike = current_price * 1.01
        nearby_strikes = sorted(
            (s for s in chain.strikes if min_strike <= s <= max_strike),
            key=lambda s: abs(s - current_price * 0.90),  # sort by proximity to ~0.90x
        )[:10]

        if not nearby_strikes:
            logger.info("No strikes in target range for %s", ticker)
            return None

        # 4. Qualify option contracts and request market data simultaneously
        options = [Option(ticker, expiry_str, s, "P", "SMART") for s in nearby_strikes]
        try:
            qualified_opts = await ibkr.ib.qualifyContractsAsync(*options)
            qualified_opts = [o for o in qualified_opts if o.conId > 0]
        except Exception as exc:
            logger.warning("Batch qualify failed for %s puts: %s — trying one-by-one", ticker, exc)
            qualified_opts = []
            for opt in options:
                try:
                    [q] = await ibkr.ib.qualifyContractsAsync(opt)
                    if q.conId > 0:
                        qualified_opts.append(q)
                except Exception:
                    pass

        if not qualified_opts:
            logger.info("No qualifying put contracts for %s", ticker)
            return None

        tickers_data = [
            ibkr.ib.reqMktData(opt, genericTickList="", snapshot=False)
            for opt in qualified_opts
        ]
        # Wait for bid/ask and modelGreeks to populate
        await asyncio.sleep(4)
        for opt in qualified_opts:
            ibkr.ib.cancelMktData(opt)

        # 5. Find option with delta closest to target (within tolerance, then best available)
        best = None
        best_delta_diff = float("inf")

        for td, opt in zip(tickers_data, qualified_opts):
            greeks = td.modelGreeks
            if not greeks:
                continue
            delta = greeks.delta
            if delta is None or math.isnan(delta):
                continue
            diff = abs(delta - cfg.target_delta)
            if diff < best_delta_diff:
                best_delta_diff = diff
                best = (td, opt, greeks)

        if not best:
            logger.info("No option with valid delta found for %s", ticker)
            return None

        td, opt, greeks = best
        strike = opt.strike

        # Log if we couldn't find a strike within tolerance (still proceed with best available)
        if best_delta_diff > cfg.delta_tolerance:
            logger.info(
                "%s: best delta %.3f is outside tolerance %.3f (target %.3f) — using anyway",
                ticker, greeks.delta, cfg.delta_tolerance, cfg.target_delta,
            )

        # 6. Compute credit: use bid if spread > $0.15, else use mid
        bid = td.bid if td.bid is not None and not math.isnan(td.bid) else None
        ask = td.ask if td.ask is not None and not math.isnan(td.ask) else None

        if bid is None or ask is None or bid <= 0:
            logger.info("No valid bid/ask for %s $%.0f put", ticker, strike)
            return None

        spread = ask - bid
        credit_per_share = bid if spread > 0.15 else (bid + ask) / 2

        if credit_per_share <= 0:
            return None

        # 7. Compute economics
        credit_total = credit_per_share * 100
        net_cost_basis = strike - credit_per_share
        assignment_cost = net_cost_basis * 100
        capital_required = strike * 100
        annualised_return = (credit_total / capital_required) * (365 / dte)

        # 8. Validate capital against per-position cap
        net_liq = ibkr.get_net_liquidation()
        if net_liq:
            bucket_capital = net_liq * config.risk.core_bucket_pct
            per_position_cap = bucket_capital * config.risk.max_position_pct_of_bucket
            if capital_required > per_position_cap:
                logger.info(
                    "%s CSP capital_required $%,.0f exceeds per-position cap $%,.0f — skipping",
                    ticker, capital_required, per_position_cap,
                )
                return None

        return CSPProposal(
            underlying=ticker,
            underlying_price=current_price,
            expiry=expiry_date.isoformat(),
            dte=dte,
            strike=strike,
            credit_per_share=round(credit_per_share, 2),
            credit_total=round(credit_total, 2),
            net_cost_basis=round(net_cost_basis, 2),
            assignment_cost=round(assignment_cost, 2),
            capital_required=round(capital_required, 2),
            annualised_return=round(annualised_return, 4),
            delta=round(greeks.delta, 3),
            ivr=candidate.ivr,
            rule_tags=["ivr_above_threshold", "delta_in_range"],
            entry_signals={
                "ivr": candidate.ivr,
                "dte": dte,
                "delta": round(greeks.delta, 3),
                "iv": round(candidate.current_iv, 4),
            },
        )

    except Exception as exc:
        logger.error("build_csp_proposal(%s) failed: %s", ticker, exc, exc_info=True)
        return None


def format_csp_trade_card(proposal: CSPProposal, proposal_id: str) -> str:
    """
    Format a CSP proposal as a Telegram trade card.

    PRD §5 M2 Trade Card Format (CSP).
    """
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
        f"\n"
        f"✅ /approve {proposal_id}    ❌ /reject {proposal_id}"
    )
