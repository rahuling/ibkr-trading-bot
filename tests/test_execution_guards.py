import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from bot import database
from bot.execution.engine import ExecutionEngine
from bot.risk.engine import RiskEngine

from conftest import FakeIBKR


async def _proposal_and_order(status="submitted", created_at=None):
    pid, oid = "P1", str(uuid.uuid4())
    card = {"strike": 400, "expiry": "20260116", "credit": 2.5, "capital_required": 40_000}
    now = datetime.now(timezone.utc)
    async with database.get_db() as db:
        await db.execute(
            "INSERT INTO proposals (proposal_id, underlying, strategy, trade_card_json, expires_at) VALUES (?,?,?,?,?)",
            (pid, "SPY", "CSP", json.dumps(card), (now + timedelta(hours=1)).isoformat()),
        )
        await db.execute(
            "INSERT INTO orders (client_order_id, proposal_id, status, expected_price, created_at) VALUES (?,?,?,?,?)",
            (oid, pid, status, 2.5, (created_at or now).isoformat()),
        )
        await db.commit()
    return pid, oid


async def test_submit_order_refuses_while_paused(config, db_path):
    risk = RiskEngine(config, FakeIBKR())
    await risk.pause("test")
    engine = ExecutionEngine(config, FakeIBKR(), risk)
    engine.is_in_blackout = lambda: False
    async with database.get_db() as db:
        with pytest.raises(RuntimeError, match="paused"):
            await engine.submit_order(db, contract=None, price=1.0, quantity=1, proposal_id="P1")
        async with db.execute("SELECT COUNT(*) FROM orders") as cur:
            assert (await cur.fetchone())[0] == 0


async def test_record_fill_is_idempotent(config, db_path):
    engine = ExecutionEngine(config, FakeIBKR(), RiskEngine(config, FakeIBKR()))
    pid, oid = await _proposal_and_order()
    async with database.get_db() as db:
        await engine._record_fill(db, oid, 2.4, pid)
        await engine._record_fill(db, oid, 2.4, pid)   # fill monitor + reconnect recovery
        async with db.execute("SELECT COUNT(*) FROM trades") as cur:
            assert (await cur.fetchone())[0] == 1


class _FakeIB:
    async def reqAllOpenOrdersAsync(self):
        return []

    async def reqExecutionsAsync(self):
        return []


async def test_reconnect_recovery_skips_fresh_pending_orders(config, db_path):
    ibkr = FakeIBKR()
    ibkr.ib = _FakeIB()
    engine = ExecutionEngine(config, ibkr, RiskEngine(config, ibkr))
    _, fresh = await _proposal_and_order(status="pending_submit")
    async with database.get_db() as db:
        await engine.recover_orphaned_orders(db, min_age_seconds=120)
        async with db.execute("SELECT status FROM orders WHERE client_order_id = ?", (fresh,)) as cur:
            assert (await cur.fetchone())["status"] == "pending_submit"
        await engine.recover_orphaned_orders(db)  # startup: no in-flight orders possible
        async with db.execute("SELECT status FROM orders WHERE client_order_id = ?", (fresh,)) as cur:
            assert (await cur.fetchone())["status"] == "cancelled"
