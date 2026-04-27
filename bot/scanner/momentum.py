"""
M1: EOD momentum scanner (3:30pm ET).

Scans the Momentum watchlist for stocks showing price and volume strength
in the final 30 minutes. Feeds M2 LeapBuilder.

Signal conditions (all must be true — PRD §5 M1 EOD Momentum Scanner):
  1. Price within 1% of intraday high
  2. Price above 20-day SMA
  3. Session volume >= 1.2x 20-day average volume
  4. No earnings within 5 calendar days

PRD reference: §5 M1 Market Scanner — EOD Momentum Scanner section.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import List, Optional

from bot.database import get_db
from bot.scanner.base import has_earnings_soon

logger = logging.getLogger(__name__)


@dataclass
class MomentumCandidate:
    ticker: str
    current_price: float
    day_high: float
    pct_from_day_high: float    # (high - price) / high * 100
    volume_ratio: float          # session_volume / avg_volume_20d
    sma20: float
    price_vs_sma20_pct: float   # (price - sma20) / sma20 * 100


class MomentumScanner:
    """
    Runs the 3:30pm EOD momentum scan.

    Returns top 3 candidates ranked by proximity to day high
    (lowest pct_from_day_high first = most momentum).
    """

    def __init__(self, config, ibkr):
        self.config = config
        self.ibkr = ibkr
        self.on_proposal = None  # set by TelegramBot.wire_scanners

    async def run(self) -> List[MomentumCandidate]:
        """Execute the EOD momentum scan and send LEAP proposals to Telegram."""
        import json
        import uuid
        from datetime import datetime, timedelta, timezone

        from bot.builder.leap import build_leap_proposal, format_leap_trade_card

        cfg = self.config.leap
        candidates = []

        for ticker in cfg.momentum_watchlist:
            try:
                if await has_earnings_soon(self.ibkr, ticker, cfg.signal.earnings_blackout_days, 0):
                    logger.debug("%s: earnings blackout — skipping", ticker)
                    continue

                async with get_db() as db:
                    async with db.execute(
                        "SELECT COUNT(*) FROM trades WHERE underlying = ? AND bucket = 'Momentum' AND status = 'open'",
                        (ticker,),
                    ) as cur:
                        row = await cur.fetchone()
                    if row and row[0] > 0:
                        logger.debug("%s: Momentum position already open — skipping", ticker)
                        continue

                snapshot = await self._get_intraday_snapshot(ticker)
                if snapshot is None:
                    continue
                await asyncio.sleep(1.5)  # IBKR historical data rate limit: 60 req / 10 min

                hist = await self._get_historical_context(ticker)
                if hist is None:
                    continue
                await asyncio.sleep(1.5)

                price = snapshot["price"]
                day_high = snapshot["day_high"]
                session_vol = snapshot.get("session_volume")
                sma20 = hist["sma20"]
                avg_vol = hist["avg_volume_20d"]

                pct_from_high = (day_high - price) / day_high * 100 if day_high > 0 else 100.0
                vol_ratio = session_vol / avg_vol if (session_vol and avg_vol and avg_vol > 0) else 0.0

                c = MomentumCandidate(
                    ticker=ticker,
                    current_price=price,
                    day_high=day_high,
                    pct_from_day_high=pct_from_high,
                    volume_ratio=vol_ratio,
                    sma20=sma20,
                    price_vs_sma20_pct=(price - sma20) / sma20 * 100 if sma20 > 0 else 0.0,
                )

                if not self._passes_signal_filter(c):
                    logger.debug(
                        "%s: failed filter — pct_from_high=%.2f%% vol_ratio=%.2f above_sma=%s",
                        ticker, pct_from_high, vol_ratio, price > sma20,
                    )
                    continue

                candidates.append(c)
                logger.info(
                    "%s: momentum candidate — price=%.2f high=%.2f pct_from_high=%.2f%% vol_ratio=%.2f",
                    ticker, price, day_high, pct_from_high, vol_ratio,
                )

            except Exception as exc:
                logger.error("MomentumScanner error on %s: %s", ticker, exc, exc_info=True)

        candidates.sort(key=lambda c: c.pct_from_day_high)
        top3 = candidates[:3]

        if not top3:
            logger.info("Momentum scan: no candidates passed all filters")
            return []

        logger.info("Momentum scan: %d candidate(s) → building LEAP proposals", len(top3))

        async with get_db() as db:
            for c in top3:
                try:
                    proposal = await build_leap_proposal(self.config, self.ibkr, c)
                    if proposal is None:
                        logger.info("No LEAP proposal built for %s", c.ticker)
                        continue

                    proposal_id = uuid.uuid4().hex[:6].upper()
                    trade_card = format_leap_trade_card(proposal, proposal_id)
                    trade_card_json = json.dumps(proposal.__dict__)

                    now = datetime.now(timezone.utc)
                    expires_at = now + timedelta(minutes=25)  # EOD entry — short TTL

                    await db.execute(
                        """INSERT INTO proposals
                           (proposal_id, underlying, strategy, trade_card_json,
                            status, created_at, expires_at)
                           VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                        (
                            proposal_id,
                            c.ticker,
                            "LEAPCall",
                            trade_card_json,
                            now.isoformat(),
                            expires_at.isoformat(),
                        ),
                    )
                    await db.commit()
                    logger.info("LEAP proposal %s saved: %s", proposal_id, c.ticker)

                    if self.on_proposal:
                        await self.on_proposal(trade_card)

                except Exception as exc:
                    logger.error("LEAP proposal build failed for %s: %s", c.ticker, exc, exc_info=True)

        return top3

    async def _get_intraday_snapshot(self, ticker: str) -> Optional[dict]:
        """
        Fetch current price, intraday high, and session volume via reqMktData.
        Returns dict with: price, day_high, session_volume — or None on failure.
        """
        from ib_async import Stock

        try:
            stock = Stock(ticker, "SMART", "USD")
            [qualified] = await self.ibkr.ib.qualifyContractsAsync(stock)
            td = self.ibkr.ib.reqMktData(qualified, genericTickList="", snapshot=False)
            await asyncio.sleep(4)
            self.ibkr.ib.cancelMktData(qualified)

            price = td.last if (td.last and not math.isnan(td.last) and td.last > 0) else None
            if price is None and td.close and not math.isnan(td.close) and td.close > 0:
                price = td.close
            if price is None:
                logger.debug("%s: no price data from snapshot", ticker)
                return None

            day_high = td.high if (td.high and not math.isnan(td.high) and td.high > 0) else price
            vol = td.volume if (td.volume and not math.isnan(td.volume) and td.volume > 0) else None

            return {"price": float(price), "day_high": float(day_high), "session_volume": float(vol) if vol else None}

        except Exception as exc:
            logger.error("_get_intraday_snapshot(%s): %s", ticker, exc)
            return None

    async def _get_historical_context(self, ticker: str) -> Optional[dict]:
        """
        Fetch 20-day SMA and average daily volume via reqHistoricalData.
        Returns dict with: sma20, avg_volume_20d — or None on failure.
        """
        from ib_async import Stock

        try:
            stock = Stock(ticker, "SMART", "USD")
            [qualified] = await self.ibkr.ib.qualifyContractsAsync(stock)
            bars = await self.ibkr.ib.reqHistoricalDataAsync(
                qualified,
                endDateTime="",
                durationStr="30 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
            )
            if not bars or len(bars) < 20:
                logger.debug("%s: insufficient history bars (%d)", ticker, len(bars) if bars else 0)
                return None

            last20 = bars[-20:]
            sma20 = sum(bar.close for bar in last20) / 20
            avg_vol = sum(bar.volume for bar in last20) / 20

            return {"sma20": float(sma20), "avg_volume_20d": float(avg_vol)}

        except Exception as exc:
            logger.error("_get_historical_context(%s): %s", ticker, exc)
            return None

    def _passes_signal_filter(self, candidate: MomentumCandidate) -> bool:
        cfg = self.config.leap.signal
        if candidate.pct_from_day_high > cfg.max_pct_from_day_high:
            return False
        if candidate.volume_ratio < cfg.min_volume_ratio:
            return False
        if cfg.require_above_sma20 and candidate.current_price < candidate.sma20:
            return False
        return True
