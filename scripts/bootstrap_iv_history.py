"""
One-time IV history bootstrap.

Run this ONCE before the first live scan to populate iv_history with
252 trading days of historical IV for all watchlist tickers.

Usage:
    cd /opt/trading-bot
    source venv/bin/activate
    python scripts/bootstrap_iv_history.py

IBKR historical data rate limit: 60 requests / 10 minutes.
This script fetches all tickers sequentially with a delay to stay under the limit.

TODO (Phase 2): implement using ib_async reqHistoricalData with
whatToShow="OPTION_IMPLIED_VOLATILITY", barSizeSetting="1 day", durationStr="365 D".
"""

import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
from bot.config import load_config
from bot.database import init_db
from bot.ibkr import create_ibkr_connection


async def bootstrap():
    load_dotenv()
    config = load_config("config.yaml")
    await init_db()

    ibkr = create_ibkr_connection()
    await ibkr.connect()

    all_tickers = (
        config.scanner.watchlist.core
        + config.scanner.watchlist.tactical
        + config.leap.momentum_watchlist
    )
    # Deduplicate while preserving order
    seen = set()
    tickers = [t for t in all_tickers if not (t in seen or seen.add(t))]

    print(f"Bootstrapping IV history for {len(tickers)} tickers: {tickers}")
    print("Rate limit: 60 requests / 10 minutes — estimated time: ~5 minutes")

    # TODO: implement historical IV fetch and insert into iv_history
    # For each ticker:
    #   bars = await ibkr.ib.reqHistoricalDataAsync(
    #       contract=Stock(ticker, "SMART", "USD"),
    #       endDateTime="",
    #       durationStr="365 D",
    #       barSizeSetting="1 day",
    #       whatToShow="OPTION_IMPLIED_VOLATILITY",
    #       useRTH=True,
    #   )
    #   for bar in bars:
    #       INSERT INTO iv_history (ticker, date, iv) VALUES (?, ?, ?)
    #       ON CONFLICT(ticker, date) DO NOTHING
    #   await asyncio.sleep(12)   # 5 tickers/minute to stay under rate limit

    print("Bootstrap complete.")
    await ibkr.disconnect()


if __name__ == "__main__":
    asyncio.run(bootstrap())
