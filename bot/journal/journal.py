"""
M7: Trade Journal.

Persistent log of every trade, Wheel cycle, and rule outcome.
Provides /journal, /wheel, /analyze, and the monthly auto-report.

PRD reference: §5 M7 Trade Journal.
"""

import logging

logger = logging.getLogger(__name__)


class Journal:
    def __init__(self, config):
        self.config = config

    async def log_trade_entry(self, db, trade: dict) -> None:
        """
        Record a new trade entry.

        Logs: underlying, strategy, legs, entry credit, IVR, delta, DTE,
        underlying price, config snapshot, rule_tags, entry_signals.

        TODO (Phase 5): implement.
        """
        raise NotImplementedError

    async def log_trade_exit(self, db, trade_id: str, exit_price: float,
                              exit_reason: str, outcome: str) -> None:
        """
        Record a trade exit with P&L and outcome.

        TODO (Phase 5): implement.
        """
        raise NotImplementedError

    async def get_journal(self, db, days: int = 30) -> str:
        """
        Return a formatted trade journal for the last N days.
        Used by /journal command.

        TODO (Phase 5): implement.
        """
        raise NotImplementedError

    async def get_wheel_cycle(self, db, cycle_id: str) -> str:
        """
        Return a formatted Wheel cycle P&L breakdown.
        Used by /wheel command.

        TODO (Phase 5): implement.
        """
        raise NotImplementedError

    async def analyze_by_tag(self, db, tag: str) -> str:
        """
        Show win rate and avg P&L for trades grouped by rule tag or signal range.
        Used by /analyze command.

        Requires >= 10 trades per segment before showing results.
        Below 10: shows data with "sample too small" caveat.

        PRD §5 M7 — Rule Performance Monitoring.

        TODO (Phase 5): implement.

        Example output for /analyze ivr:
            IVR 30-39:  12 trades  Win: 58%  Avg P&L: +$118
            IVR 40-49:   9 trades  Win: 78%  Avg P&L: +$187
            IVR 50+:     5 trades  Win: 80%  Avg P&L: +$201
        """
        raise NotImplementedError

    async def send_monthly_report(self, telegram_bot) -> None:
        """
        Auto-generate and send the monthly P&L report.
        Scheduled on the 1st of each month at 8am ET.

        Includes: trades, win rate, P&L by strategy, Wheel cycle stats,
        avg slippage, capital start/end, monthly return.

        PRD §5 M7 — Monthly Report.

        TODO (Phase 5): implement.
        """
        raise NotImplementedError
