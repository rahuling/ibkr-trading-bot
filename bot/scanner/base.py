"""
Shared scanner utilities.

Used by both the premium scanner (9:45am/3pm) and the momentum scanner (3:30pm).
"""

import asyncio
import logging
import math
from datetime import date, timedelta
from typing import Optional

import aiosqlite

from bot.database import get_db

logger = logging.getLogger(__name__)


async def get_current_iv(ibkr, ticker: str) -> Optional[float]:
    """
    Fetch current implied volatility for a ticker via IBKR.

    Returns IV as a decimal (e.g. 0.42 for 42%), or None if unavailable.
    Uses genericTickList="106" (optionImpliedVol) via reqMktData.
    Requires a live market data subscription during market hours.
    """
    from ib_async import Stock
    try:
        stock = Stock(ticker, "SMART", "USD")
        [qualified] = await ibkr.ib.qualifyContractsAsync(stock)
        ticker_data = ibkr.ib.reqMktData(qualified, genericTickList="106", snapshot=False)
        # Wait for tick 106 to arrive
        await asyncio.sleep(4)
        iv = ticker_data.impliedVolatility
        ibkr.ib.cancelMktData(qualified)

        if iv is None or math.isnan(iv):
            logger.warning("No IV data returned for %s", ticker)
            return None
        return float(iv)
    except Exception as exc:
        logger.error("get_current_iv(%s) failed: %s", ticker, exc)
        return None


async def compute_ivr(
    db: aiosqlite.Connection, ticker: str, current_iv: float
) -> Optional[float]:
    """
    Compute IV Rank from the local iv_history table.

    IVR = (current_IV - 52wk_low) / (52wk_high - 52wk_low) * 100

    Returns None if fewer than 252 rows exist for this ticker (insufficient history).
    Caller should bootstrap iv_history before the first scan.
    """
    cutoff = (date.today() - timedelta(days=365)).isoformat()
    async with db.execute(
        "SELECT MIN(iv), MAX(iv), COUNT(*) FROM iv_history "
        "WHERE ticker = ? AND date >= ?",
        (ticker, cutoff),
    ) as cursor:
        row = await cursor.fetchone()

    if not row or row[2] < 252:
        logger.debug(
            "Insufficient IV history for %s (%s rows, need 252)",
            ticker, row[2] if row else 0,
        )
        return None

    iv_low, iv_high = row[0], row[1]
    if iv_high <= iv_low:
        return None

    ivr = (current_iv - iv_low) / (iv_high - iv_low) * 100
    return round(ivr, 1)


async def has_earnings_soon(
    ibkr, ticker: str, pre_days: int, post_days: int
) -> bool:
    """
    Check if the ticker has earnings within the blackout window.

    Uses yfinance earnings calendar (free, no IBKR fundamental subscription needed).
    Returns False on any failure — human review at L2 approval acts as the safety net.
    """
    try:
        import yfinance as yf
        t = yf.Ticker(ticker)
        earnings_df = t.get_earnings_dates(limit=8)
        if earnings_df is None or earnings_df.empty:
            return False

        today = date.today()
        for ed_ts in earnings_df.index:
            try:
                ed = ed_ts.date() if hasattr(ed_ts, "date") else date.fromisoformat(str(ed_ts)[:10])
                days_delta = (ed - today).days
                if -post_days <= days_delta <= pre_days:
                    logger.info(
                        "%s earnings on %s — within blackout window (%d pre / %d post)",
                        ticker, ed, pre_days, post_days,
                    )
                    return True
            except Exception:
                continue
        return False
    except Exception as exc:
        logger.warning("has_earnings_soon(%s) failed: %s — allowing ticker", ticker, exc)
        return False


async def update_iv_history(config, ibkr) -> None:
    """
    Append today's closing IV for all watchlist tickers to iv_history.

    Scheduled at 4:15pm ET daily. Idempotent — skips if today's row already exists.
    Uses reqHistoricalData(whatToShow="OPTION_IMPLIED_VOLATILITY") to get closing IV.

    IBKR rate limit: 60 historical data requests / 10 minutes.
    Inserts 1 row per ticker with ~1s delay between tickers (safe for watchlists up to ~60 tickers).
    """
    from ib_async import Stock

    tickers = list(dict.fromkeys(
        config.scanner.watchlist.core
        + config.scanner.watchlist.tactical
        + config.leap.momentum_watchlist
    ))
    today_str = date.today().isoformat()

    async with get_db() as db:
        for ticker in tickers:
            try:
                async with db.execute(
                    "SELECT 1 FROM iv_history WHERE ticker = ? AND date = ?",
                    (ticker, today_str),
                ) as cursor:
                    if await cursor.fetchone():
                        logger.debug("IV already recorded for %s on %s", ticker, today_str)
                        continue

                stock = Stock(ticker, "SMART", "USD")
                [qualified] = await ibkr.ib.qualifyContractsAsync(stock)
                bars = await ibkr.ib.reqHistoricalDataAsync(
                    qualified,
                    endDateTime="",
                    durationStr="1 D",
                    barSizeSetting="1 day",
                    whatToShow="OPTION_IMPLIED_VOLATILITY",
                    useRTH=True,
                )
                if not bars:
                    logger.warning("No IV bar returned for %s", ticker)
                    continue

                iv = float(bars[-1].close)
                await db.execute(
                    "INSERT OR IGNORE INTO iv_history (ticker, date, iv) VALUES (?, ?, ?)",
                    (ticker, today_str, iv),
                )
                await db.commit()
                logger.info("IV history updated: %s = %.4f on %s", ticker, iv, today_str)

                await asyncio.sleep(1.2)

            except Exception as exc:
                logger.error("update_iv_history(%s) failed: %s", ticker, exc)


async def bootstrap_iv_history(config, ibkr) -> None:
    """
    One-time bootstrap: pull 365 calendar days of IV history for all watchlist tickers.

    Run once before the first live scan via scripts/bootstrap_iv_history.py.
    Rate limit: 60 IBKR historical data requests / 10 minutes — 10s between tickers
    keeps us well under the limit (6 tickers/min = 36 req/10 min).
    """
    from ib_async import Stock

    tickers = list(dict.fromkeys(
        config.scanner.watchlist.core
        + config.scanner.watchlist.tactical
        + config.leap.momentum_watchlist
    ))

    async with get_db() as db:
        for ticker in tickers:
            try:
                stock = Stock(ticker, "SMART", "USD")
                [qualified] = await ibkr.ib.qualifyContractsAsync(stock)
                bars = await ibkr.ib.reqHistoricalDataAsync(
                    qualified,
                    endDateTime="",
                    durationStr="365 D",
                    barSizeSetting="1 day",
                    whatToShow="OPTION_IMPLIED_VOLATILITY",
                    useRTH=True,
                )
                if not bars:
                    logger.warning("No IV data bootstrapped for %s", ticker)
                    continue

                rows = []
                for bar in bars:
                    d = bar.date
                    date_str = d.date().isoformat() if hasattr(d, "date") else str(d)[:10]
                    rows.append((ticker, date_str, float(bar.close)))

                await db.executemany(
                    "INSERT OR IGNORE INTO iv_history (ticker, date, iv) VALUES (?, ?, ?)",
                    rows,
                )
                await db.commit()
                logger.info("Bootstrapped %d IV rows for %s", len(rows), ticker)

                # 10s between tickers — 6 per minute, 36 per 10 minutes (under 60 limit)
                await asyncio.sleep(10)

            except Exception as exc:
                logger.error("bootstrap_iv_history(%s) failed: %s", ticker, exc)
