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

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, date
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LEAPProposal:
    underlying: str
    underlying_price: float     # spot price at scan time
    expiry: str                 # "YYYY-MM-DD"
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
      2. Pick the expiry closest to the midpoint of that range
      3. Filter strikes: ITM calls between 70%–95% of current price
      4. Iterate strikes until one has delta within (target ± tolerance)
         and extrinsic <= max_extrinsic_pct
      5. Compute stop_price and profit_target_price from underlying_price
      6. Return LEAPProposal or None if no valid LEAP found
    """
    from ib_async import Option, Stock

    cfg = config.leap
    ticker = candidate.ticker
    price = candidate.current_price

    try:
        stock = Stock(ticker, "SMART", "USD")
        [qs] = await ibkr.ib.qualifyContractsAsync(stock)
    except Exception as exc:
        logger.error("build_leap_proposal qualify %s: %s", ticker, exc)
        return None

    chains = await ibkr.ib.reqSecDefOptParamsAsync(qs.symbol, "", qs.secType, qs.conId)
    if not chains:
        logger.info("build_leap_proposal %s: no option chain", ticker)
        return None

    chain = chains[0]
    today = date.today()

    # 1. Find expiries in DTE range
    valid = []
    for e in chain.expirations:
        try:
            exp_date = datetime.strptime(e, "%Y%m%d").date()
            dte = (exp_date - today).days
            if cfg.min_dte <= dte <= cfg.max_dte:
                valid.append((dte, e, exp_date))
        except ValueError:
            continue

    if not valid:
        logger.info("build_leap_proposal %s: no expiry in %d-%d DTE range", ticker, cfg.min_dte, cfg.max_dte)
        return None

    # 2. Pick expiry closest to midpoint of DTE range
    target_dte = (cfg.min_dte + cfg.max_dte) // 2
    valid.sort(key=lambda x: abs(x[0] - target_dte))
    best_dte, best_exp_str, best_exp_date = valid[0]

    # 3. Candidate strikes: ITM calls (70%–95% of spot)
    min_strike = price * 0.70
    max_strike = price * 0.95
    candidate_strikes = sorted(
        (s for s in chain.strikes if min_strike <= s <= max_strike),
        reverse=True,  # start closest-to-money (higher delta), work deeper
    )

    if not candidate_strikes:
        logger.info("build_leap_proposal %s: no strikes in ITM range [%.0f, %.0f]", ticker, min_strike, max_strike)
        return None

    # 4. Iterate strikes: find first with matching delta and extrinsic threshold
    for strike in candidate_strikes:
        try:
            opt = Option(ticker, best_exp_str, strike, "C", "SMART")
            try:
                [qopt] = await ibkr.ib.qualifyContractsAsync(opt)
            except Exception:
                continue

            td = ibkr.ib.reqMktData(qopt, genericTickList="", snapshot=False)
            await asyncio.sleep(3)
            ibkr.ib.cancelMktData(qopt)

            ask = td.ask if (td.ask and not math.isnan(td.ask) and td.ask > 0) else None
            if ask is None:
                continue

            if not td.modelGreeks or not td.modelGreeks.delta:
                continue
            delta = td.modelGreeks.delta
            if math.isnan(delta):
                continue

            if abs(delta - cfg.target_delta) > cfg.delta_tolerance:
                continue

            intrinsic = max(0.0, price - strike) * 100
            cost = ask * 100
            extrinsic = cost - intrinsic
            extrinsic_pct = extrinsic / cost if cost > 0 else 1.0

            if extrinsic_pct > cfg.max_extrinsic_pct:
                logger.debug(
                    "%s strike %.0f: extrinsic %.0f%% > max %.0f%% — skipping",
                    ticker, strike, extrinsic_pct * 100, cfg.max_extrinsic_pct * 100,
                )
                continue

            # 5. Compute stops / targets on UNDERLYING price
            stop_price          = round(price * (1 - cfg.stop_loss_pct), 2)
            profit_target_price = round(price * (1 + cfg.profit_target_pct), 2)
            stop_dist           = price - stop_price
            realistic_risk      = round(stop_dist * delta * 100, 0)

            rule_tags = ["eod_momentum"]
            if candidate.pct_from_day_high < 0.5:
                rule_tags.append("near_day_high")
            if candidate.volume_ratio >= 1.5:
                rule_tags.append("high_volume")
            if candidate.current_price > candidate.sma20:
                rule_tags.append("above_sma20")

            logger.info(
                "LEAP proposal built: %s %s %s C  delta=%.2f  ask=%.2f  cost=$%.0f  extrinsic=%.0f%%",
                ticker, best_exp_str, strike, delta, ask, cost, extrinsic_pct * 100,
            )

            return LEAPProposal(
                underlying=ticker,
                underlying_price=price,
                expiry=best_exp_date.isoformat(),
                dte=best_dte,
                strike=strike,
                ask_price=ask,
                cost_total=cost,
                intrinsic_value=round(intrinsic, 2),
                extrinsic_value=round(extrinsic, 2),
                extrinsic_pct=round(extrinsic_pct, 4),
                delta=delta,
                stop_price=stop_price,
                profit_target_price=profit_target_price,
                realistic_risk=realistic_risk,
                pct_from_day_high=candidate.pct_from_day_high,
                volume_ratio=candidate.volume_ratio,
                rule_tags=rule_tags,
                entry_signals={
                    "delta": round(delta, 3),
                    "dte": best_dte,
                    "volume_ratio": round(candidate.volume_ratio, 2),
                    "pct_from_day_high": round(candidate.pct_from_day_high, 2),
                    "extrinsic_pct": round(extrinsic_pct, 3),
                    "sma20": round(candidate.sma20, 2),
                },
            )

        except Exception as exc:
            logger.warning("build_leap_proposal %s strike %.0f: %s", ticker, strike, exc)
            continue

    logger.info("build_leap_proposal %s: no strike satisfied delta/extrinsic constraints", ticker)
    return None


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
