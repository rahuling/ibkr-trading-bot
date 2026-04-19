"""
M2: Bull Put Spread trade builder.

Constructs a fully-specified spread proposal and the IBKR BAG/combo
contract required for atomic two-leg submission.

PRD reference: §5 M2 Trade Builder — Logic: Bull Put Spread.

IMPORTANT: Spread legs are submitted as a single BAG (combo) order —
never as two separate orders. Separate leg submission creates leg risk.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime
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
    short_put_con_id: int
    long_put_con_id: int
    ivr: float
    rule_tags: list
    entry_signals: dict


async def build_spread_proposal(config, ibkr, candidate) -> Optional[SpreadProposal]:
    """
    Build a Bull Put Spread proposal.

    Steps (PRD §5 M2 Logic — Bull Put Spread):
      1. Fetch option chain definition (valid expiries + strikes)
      2. Select expiry closest to midpoint of DTE range (7–21 days)
      3. Filter candidate strikes: 0.70x–1.01x of current price
      4. Request market data for up to 10 candidate puts simultaneously
      5. Short leg: strike closest to target_delta (default -0.30)
      6. Long leg: spread_width points below short strike (default 5 points)
      7. Fetch market data for long put separately
      8. Compute credit, max_loss, credit_to_width_ratio
      9. Validate: max_loss <= max_spread_loss; credit_to_width >= min_credit_to_width_ratio
    """
    from ib_async import Option, Stock

    cfg = config.trading.spread
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

        # 3. Filter candidate strikes for short leg search
        # Include extra room below current price to cover the long leg too
        min_strike = current_price * 0.70 - cfg.spread_width
        max_strike = current_price * 1.01
        nearby_strikes = sorted(
            (s for s in chain.strikes if min_strike <= s <= max_strike),
            key=lambda s: abs(s - current_price * 0.88),  # centre near ~-0.30 delta area
        )[:10]

        if not nearby_strikes:
            logger.info("No strikes in target range for %s", ticker)
            return None

        # 4. Qualify and request market data for candidate short-leg options
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
        await asyncio.sleep(4)
        for opt in qualified_opts:
            ibkr.ib.cancelMktData(opt)

        # 5. Find short leg: delta closest to target (within tolerance, then best available)
        best_short = None
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
                best_short = (td, opt, greeks)

        if not best_short:
            logger.info("No option with valid delta found for %s", ticker)
            return None

        short_td, short_opt, short_greeks = best_short
        short_strike = short_opt.strike
        long_strike = short_strike - cfg.spread_width

        # Validate long strike exists in chain
        if long_strike not in chain.strikes:
            # Find closest available strike below short_strike
            below = [s for s in chain.strikes if s < short_strike]
            if not below:
                logger.info("No long leg strike available below %.1f for %s", short_strike, ticker)
                return None
            long_strike = max(below)  # closest available

        actual_width = short_strike - long_strike

        # Short leg bid/ask
        short_bid = short_td.bid if short_td.bid is not None and not math.isnan(short_td.bid) else None
        short_ask = short_td.ask if short_td.ask is not None and not math.isnan(short_td.ask) else None
        if short_bid is None or short_ask is None or short_bid <= 0:
            logger.info("No valid bid/ask for %s short leg $%.0f", ticker, short_strike)
            return None

        short_spread = short_ask - short_bid
        short_credit = short_bid if short_spread > 0.15 else (short_bid + short_ask) / 2

        # 7. Fetch market data for long leg separately
        long_opt = Option(ticker, expiry_str, long_strike, "P", "SMART")
        try:
            [qualified_long] = await ibkr.ib.qualifyContractsAsync(long_opt)
        except Exception as exc:
            logger.info("Could not qualify long leg for %s: %s", ticker, exc)
            return None

        long_td = ibkr.ib.reqMktData(qualified_long, genericTickList="", snapshot=False)
        await asyncio.sleep(3)
        ibkr.ib.cancelMktData(qualified_long)

        long_bid = long_td.bid if long_td.bid is not None and not math.isnan(long_td.bid) else None
        long_ask = long_td.ask if long_td.ask is not None and not math.isnan(long_td.ask) else None
        if long_bid is None or long_ask is None or long_ask <= 0:
            logger.info("No valid bid/ask for %s long leg $%.0f", ticker, long_strike)
            return None

        long_debit = long_ask if (long_ask - long_bid) > 0.15 else (long_bid + long_ask) / 2

        # 8. Net credit for the spread
        credit_per_share = short_credit - long_debit
        if credit_per_share <= 0:
            logger.info("%s spread has non-positive credit (%.2f) — skipping", ticker, credit_per_share)
            return None

        credit_total = credit_per_share * 100
        max_loss = (actual_width - credit_per_share) * 100
        max_return_pct = credit_per_share / (actual_width - credit_per_share)
        credit_to_width_ratio = credit_per_share / actual_width
        breakeven = short_strike - credit_per_share

        # 9. Validate
        if max_loss > config.risk.max_spread_loss:
            logger.info(
                "%s max_loss $%.0f exceeds limit $%.0f — skipping",
                ticker, max_loss, config.risk.max_spread_loss,
            )
            return None

        if credit_to_width_ratio < cfg.min_credit_to_width_ratio:
            logger.info(
                "%s credit/width %.1f%% below minimum %.1f%% — skipping",
                ticker,
                credit_to_width_ratio * 100,
                cfg.min_credit_to_width_ratio * 100,
            )
            return None

        return SpreadProposal(
            underlying=ticker,
            underlying_price=current_price,
            expiry=expiry_date.isoformat(),
            dte=dte,
            short_strike=short_strike,
            long_strike=long_strike,
            spread_width=actual_width,
            credit_per_share=round(credit_per_share, 2),
            credit_total=round(credit_total, 2),
            max_loss=round(max_loss, 2),
            max_return_pct=round(max_return_pct, 4),
            credit_to_width_ratio=round(credit_to_width_ratio, 4),
            breakeven=round(breakeven, 2),
            short_delta=round(short_greeks.delta, 3),
            short_put_con_id=short_opt.conId,
            long_put_con_id=qualified_long.conId,
            ivr=candidate.ivr,
            rule_tags=["ivr_above_threshold", "delta_in_range", "credit_width_ratio_met"],
            entry_signals={
                "ivr": candidate.ivr,
                "dte": dte,
                "short_delta": round(short_greeks.delta, 3),
                "credit_to_width": round(credit_to_width_ratio, 4),
                "iv": round(candidate.current_iv, 4),
            },
        )

    except Exception as exc:
        logger.error("build_spread_proposal(%s) failed: %s", ticker, exc, exc_info=True)
        return None


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
        f"Width:  ${proposal.spread_width:.0f}\n"
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
