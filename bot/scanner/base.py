"""
Shared scanner utilities.

Used by both the premium scanner (9:45am/3pm) and the momentum scanner (3:30pm).
"""

import logging
from datetime import date, timedelta
from typing import Optional

logger = logging.getLogger(__name__)


async def get_current_iv(ibkr, ticker: str) -> Optional[float]:
    """
    Fetch current implied volatility for a ticker via IBKR.

    Returns IV as a decimal (e.g. 0.42 for 42%), or None if unavailable.

    TODO (Phase 2): implement using ib_async reqMktData with genericTickList
    that includes IV (tick type 24 = impliedVolatility).
    """
    raise NotImplementedError


async def compute_ivr(db, ticker: str, current_iv: float) -> Optional[float]:
    """
    Compute IV Rank from the local iv_history table.

    IVR = (current_IV - 52wk_low) / (52wk_high - 52wk_low) * 100

    Requires iv_history to have >= 252 rows for this ticker.
    Returns None if insufficient history.

    TODO (Phase 2): query iv_history, compute IVR.
    """
    raise NotImplementedError


async def has_earnings_soon(
    ibkr, ticker: str, pre_days: int, post_days: int
) -> bool:
    """
    Check if the ticker has earnings within the blackout window.

    Checks both:
    - pre_days before earnings (avoid entering before IV spike)
    - post_days after earnings (avoid entering during IV crush)

    TODO (Phase 2): fetch earnings date via IBKR fundamental data or
    Nasdaq calendar API. Return True if earnings fall within the window.
    """
    raise NotImplementedError


async def update_iv_history(config, ibkr) -> None:
    """
    Append today's closing IV for all watchlist tickers to iv_history.

    Scheduled at 4:15pm ET daily.

    TODO (Phase 2): for each ticker in core + tactical + momentum watchlists,
    fetch today's closing IV via reqHistoricalData(whatToShow="OPTION_IMPLIED_VOLATILITY")
    and insert into iv_history. Skip if today's row already exists (idempotent).
    """
    raise NotImplementedError


async def bootstrap_iv_history(config, ibkr) -> None:
    """
    One-time bootstrap: pull 252 trading days of IV history for all watchlist tickers.

    Run once before the first live scan. Rate limit: 60 IBKR historical data
    requests per 10 minutes — spread ticker fetches with a delay.

    See scripts/bootstrap_iv_history.py for the standalone runner.
    """
    raise NotImplementedError
