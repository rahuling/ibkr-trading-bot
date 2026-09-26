from datetime import datetime, timezone

import pytest

from bot.database import get_state
from bot.risk.engine import PAUSE_STATE_KEY, RiskCheckResult, RiskEngine, period_starts

from conftest import FakeIBKR, insert_trade


# ── Fail closed ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("net_liq", [None, 0.0])
async def test_new_trade_blocked_when_portfolio_value_unavailable(config, db_path, net_liq):
    engine = RiskEngine(config, FakeIBKR(net_liq))
    status = await engine.check_new_trade("SPY", 1_000, "Core")
    assert status.result == RiskCheckResult.BLOCKED
    assert "failing closed" in status.reason


@pytest.mark.parametrize("check", ["check_loss_limits", "check_underlying_exposure", "check_pdt"])
async def test_sub_checks_block_without_portfolio_value(config, db_path, check):
    engine = RiskEngine(config, FakeIBKR(None))
    fn = getattr(engine, check)
    status = fn() if check == "check_pdt" else (await fn("SPY") if check == "check_underlying_exposure" else await fn())
    assert status.result == RiskCheckResult.BLOCKED


async def test_new_trade_ok_with_healthy_account(config, db_path, ibkr):
    engine = RiskEngine(config, ibkr)
    status = await engine.check_new_trade("SPY", 5_000, "Core")
    assert status.result == RiskCheckResult.OK, status.reason


# ── Limits ───────────────────────────────────────────────────────────────────

async def test_per_position_cap(config, db_path, ibkr):
    engine = RiskEngine(config, ibkr)
    # Core budget 55k, 33% per position → ~18.15k
    status = await engine.check_new_trade("SPY", 20_000, "Core")
    assert status.result == RiskCheckResult.BLOCKED and "per-position cap" in status.reason


async def test_bucket_capacity(config, db_path, ibkr):
    engine = RiskEngine(config, ibkr)
    for u in ("AAPL", "MSFT", "NVDA"):
        await insert_trade(underlying=u, legs=[{"strike": 170, "action": "SELL"}])  # 17k each
    status = await engine.check_new_trade("GOOGL", 5_000, "Core")
    assert status.result == RiskCheckResult.BLOCKED and "bucket full" in status.reason


async def test_underlying_exposure(config, db_path, ibkr):
    engine = RiskEngine(config, ibkr)
    for _ in range(3):
        await insert_trade(underlying="SPY", legs=[{"strike": 150, "action": "SELL"}])  # 45k > 40k
    status = await engine.check_underlying_exposure("SPY")
    assert status.result == RiskCheckResult.BLOCKED


async def test_spread_capital_is_width(config, db_path, ibkr):
    from bot.risk.engine import _capital_from_legs
    legs = [{"strike": 100, "action": "SELL"}, {"strike": 95, "action": "BUY"}]
    assert _capital_from_legs(legs) == 500


async def test_pdt_thresholds(config, db_path):
    assert RiskEngine(config, FakeIBKR(24_000)).check_pdt().result == RiskCheckResult.BLOCKED
    assert RiskEngine(config, FakeIBKR(28_000)).check_pdt().result == RiskCheckResult.WARNING
    assert RiskEngine(config, FakeIBKR(50_000)).check_pdt().result == RiskCheckResult.OK


# ── Loss limits + persisted pause ────────────────────────────────────────────

async def test_daily_loss_limit_pauses_notifies_once_and_persists(config, db_path, ibkr):
    engine = RiskEngine(config, ibkr)
    sent = []

    async def notify(msg):
        sent.append(msg)
    engine.on_notify = notify

    await insert_trade(status="closed", pnl=-2_000, exit_date=datetime.now(timezone.utc))  # 2% > 1.5%
    first = await engine.check_loss_limits()
    second = await engine.check_loss_limits()
    assert first.result == second.result == RiskCheckResult.BLOCKED
    assert engine.is_paused
    assert len(sent) == 1  # no alert spam on repeated checks
    assert await get_state(PAUSE_STATE_KEY) == "Daily loss limit breached"

    # Simulated restart: a fresh engine restores the pause from the DB.
    restarted = RiskEngine(config, ibkr)
    assert await restarted.load_state() == "Daily loss limit breached"
    status = await restarted.check_new_trade("SPY", 1_000, "Core")
    assert status.result == RiskCheckResult.BLOCKED and "paused" in status.reason


async def test_resume_clears_persisted_pause(config, db_path, ibkr):
    engine = RiskEngine(config, ibkr)
    await engine.pause("User command /pause")
    await engine.resume()
    assert not engine.is_paused
    assert await get_state(PAUSE_STATE_KEY) is None
    assert await RiskEngine(config, ibkr).load_state() is None


async def test_losses_before_et_midnight_do_not_count_today(config, db_path, ibkr, monkeypatch):
    # 2026-03-10 02:00 UTC is 22:00 ET on 03-09 → belongs to the previous ET day.
    now = datetime(2026, 3, 10, 15, 0, tzinfo=timezone.utc)
    monkeypatch.setattr("bot.risk.engine.period_starts", lambda: period_starts(now))
    await insert_trade(status="closed", pnl=-2_000, exit_date=datetime(2026, 3, 10, 2, 0, tzinfo=timezone.utc))
    engine = RiskEngine(config, ibkr)
    summary = await engine.get_risk_summary()
    assert summary["daily_pnl"] == 0
    assert summary["weekly_pnl"] == -2_000  # still inside the ET week (Mon 03-09)


# ── Period boundaries ────────────────────────────────────────────────────────

def test_period_starts_use_eastern_midnight_standard_time():
    day, week, month = period_starts(datetime(2026, 1, 15, 3, 0, tzinfo=timezone.utc))  # 22:00 ET Jan 14 (Wed)
    assert day == datetime(2026, 1, 14, 5, 0, tzinfo=timezone.utc)
    assert week == datetime(2026, 1, 12, 5, 0, tzinfo=timezone.utc)   # Monday
    assert month == datetime(2026, 1, 1, 5, 0, tzinfo=timezone.utc)


def test_period_starts_daylight_saving():
    day, _, month = period_starts(datetime(2026, 7, 4, 12, 0, tzinfo=timezone.utc))
    assert day == datetime(2026, 7, 4, 4, 0, tzinfo=timezone.utc)
    assert month == datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc)


def test_month_start_across_dst_change():
    # March 2026: month starts in EST (UTC-5) even though "now" is in EDT.
    _, _, month = period_starts(datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc))
    assert month == datetime(2026, 3, 1, 5, 0, tzinfo=timezone.utc)
