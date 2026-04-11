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

import logging
from dataclasses import dataclass
from typing import List, Optional

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

    async def run(self) -> List[MomentumCandidate]:
        """
        Execute the EOD momentum scan.

        Steps:
          1. For each ticker in momentum_watchlist:
             a. Fetch current price, intraday high via reqMktData
             b. Fetch 20-day historical data for SMA + avg volume
             c. Compute signal metrics
          2. Apply all signal filters
          3. Filter: earnings within 5 days
          4. Filter: Momentum bucket has capacity
          5. Rank by pct_from_day_high ascending (closest to high = most momentum)
          6. Return top 3

        TODO (Phase 5b): implement each step.
        Sends results to Telegram via notifications module.
        """
        raise NotImplementedError

    async def _get_intraday_snapshot(self, ticker: str) -> Optional[dict]:
        """
        Fetch current price, intraday high, and session volume.

        Uses ib_async reqMktData snapshot (not streaming).
        Returns dict with: price, day_high, session_volume, or None on failure.

        TODO (Phase 5b)
        """
        raise NotImplementedError

    async def _get_historical_context(self, ticker: str) -> Optional[dict]:
        """
        Fetch 20-day SMA and average daily volume.

        Uses ib_async reqHistoricalData(barSizeSetting="1 day", durationStr="30 D").
        Returns dict with: sma20, avg_volume_20d, or None on failure.

        Note: IBKR historical data is rate-limited at 60 requests / 10 minutes.
        Add a short delay between tickers when scanning the full watchlist.

        TODO (Phase 5b)
        """
        raise NotImplementedError

    def _passes_signal_filter(self, candidate: MomentumCandidate) -> bool:
        """
        Apply all signal filters from config.leap.signal.

        Returns True if the candidate passes all conditions.
        """
        cfg = self.config.leap.signal
        if candidate.pct_from_day_high > cfg.max_pct_from_day_high:
            return False
        if candidate.volume_ratio < cfg.min_volume_ratio:
            return False
        if cfg.require_above_sma20 and candidate.current_price < candidate.sma20:
            return False
        return True
