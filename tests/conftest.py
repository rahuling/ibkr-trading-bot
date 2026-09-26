import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bot import database  # noqa: E402
from bot.config import load_config  # noqa: E402


@pytest.fixture
def config():
    return load_config(str(ROOT / "config.yaml"))


@pytest.fixture
async def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.db"
    monkeypatch.setattr(database, "DB_PATH", path)
    await database.init_db()
    return path


class FakeIBKR:
    def __init__(self, net_liq=100_000.0):
        self.net_liq = net_liq

    def get_net_liquidation(self):
        return self.net_liq


@pytest.fixture
def ibkr():
    return FakeIBKR()


async def insert_trade(*, underlying="SPY", bucket="Core", strategy="CSP", legs=None,
                       status="open", pnl=None, exit_date=None):
    legs = legs or [{"strike": 400, "expiry": "20260116", "right": "P", "qty": 1, "action": "SELL"}]
    async with database.get_db() as db:
        await db.execute(
            """INSERT INTO trades (trade_id, underlying, strategy, bucket, legs, entry_date,
                                   status, pnl, exit_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), underlying, strategy, bucket, json.dumps(legs),
             datetime.now(timezone.utc).isoformat(), status, pnl,
             exit_date.isoformat() if exit_date else None),
        )
        await db.commit()
