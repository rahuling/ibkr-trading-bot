"""
One-time IV history bootstrap.

Run this ONCE before the first live scan to populate iv_history with
~252 trading days of historical IV for all watchlist tickers.

Usage:
    cd /opt/trading-bot
    source venv/bin/activate
    python scripts/bootstrap_iv_history.py

IBKR historical data rate limit: 60 requests / 10 minutes.
This script fetches tickers sequentially with a 10s delay between each
(6 tickers/min = 36 req/10 min — well under the limit).

Estimated time: ~10s per ticker (e.g. 20 tickers = ~3.5 minutes).
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

from bot.config import load_config
from bot.database import init_db
from bot.ibkr import create_ibkr_connection
from bot.scanner.base import bootstrap_iv_history


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
    seen: set = set()
    tickers = [t for t in all_tickers if not (t in seen or seen.add(t))]

    print(f"Bootstrapping IV history for {len(tickers)} tickers: {tickers}")
    print("Rate limit: ~6 tickers/minute — estimated time: "
          f"~{len(tickers) * 10 // 60}m {len(tickers) * 10 % 60}s")

    await bootstrap_iv_history(config, ibkr)

    print("Bootstrap complete. Run /scan to verify.")
    await ibkr.disconnect()


if __name__ == "__main__":
    asyncio.run(bootstrap())
