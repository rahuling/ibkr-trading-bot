"""
M7: Trade Journal.

All data lives in the trades / wheel_cycles tables — this module is
pure read + format. Writes happen in the execution engine.

/journal [days] — recent closed trades
/wheel [id]     — full Wheel cycle breakdown
/analyze [dim]  — win rate by IVR band / DTE band / strategy / rule tag
Monthly report  — auto-sent on the 1st of each month at 8am ET

PRD reference: §5 M7 Trade Journal.
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_OUTCOME_ICON = {
    "Win":       "✅",
    "Loss":      "❌",
    "BreakEven": "➖",
    "Assigned":  "📥",
    "Rolled":    "🔁",
    "Scratch":   "➖",
}


def _fmt_pnl(pnl) -> str:
    if pnl is None:
        return "?"
    pnl = float(pnl)
    sign = "+" if pnl >= 0 else ""
    return f"{sign}${pnl:.0f}"


def _ivr_band(ivr) -> str:
    if ivr is None:
        return "IVR N/A"
    v = float(ivr)
    if v < 30:  return "IVR <30"
    if v < 40:  return "IVR 30-39"
    if v < 50:  return "IVR 40-49"
    if v < 60:  return "IVR 50-59"
    return "IVR 60+"


def _dte_band(dte) -> str:
    if dte is None:
        return "DTE N/A"
    d = int(dte)
    if d < 21:  return "DTE <21"
    if d < 31:  return "DTE 21-30"
    if d < 46:  return "DTE 31-45"
    return "DTE 46+"


def _leg_header(t: dict) -> str:
    legs = json.loads(t["legs"]) if isinstance(t["legs"], str) else t["legs"]
    short = next((l for l in legs if l.get("action") == "SELL"), None)
    long_leg = next((l for l in legs if l.get("action") == "BUY"), None)
    s = t["strategy"]
    und = t["underlying"]
    if s == "CSP":
        return f"{und} CSP ${short['strike']:.0f}P" if short else f"{und} CSP"
    if s == "BullPutSpread":
        return (f"{und} BPS ${short['strike']:.0f}/${long_leg['strike']:.0f}"
                if short and long_leg else f"{und} BPS")
    if s == "CoveredCall":
        return f"{und} CC ${short['strike']:.0f}C" if short else f"{und} CC"
    if s == "LEAPCall":
        return f"{und} LEAP ${short['strike']:.0f}C" if short else f"{und} LEAP"
    return f"{und} {s}"


def _analyze_groups(groups: dict, header: str) -> str:
    lines = [header, "──────────────────────"]
    for band in sorted(groups):
        g = groups[band]
        wins = sum(1 for t in g if t.get("outcome") in ("Win", "Assigned"))
        total = sum(float(t.get("pnl") or 0) for t in g)
        avg = total / len(g) if g else 0
        caveat = "  ⚠️ small sample" if len(g) < 10 else ""
        lines.append(
            f"  {band:<12}  {len(g):>2} trades  "
            f"Win: {wins / len(g) * 100:.0f}%  "
            f"Avg P&L: {_fmt_pnl(avg)}{caveat}"
        )
    return "\n".join(lines)


class Journal:
    def __init__(self, config):
        self.config = config

    # ------------------------------------------------------------------
    # Entry / exit logging — no-ops; execution engine writes the rows
    # ------------------------------------------------------------------

    async def log_trade_entry(self, db, trade: dict) -> None:
        pass

    async def log_trade_exit(self, db, trade_id: str, exit_price: float,
                              exit_reason: str, outcome: str) -> None:
        pass

    # ------------------------------------------------------------------
    # /journal [days]
    # ------------------------------------------------------------------

    async def get_journal(self, db, days: int = 30) -> str:
        since = datetime.now(timezone.utc) - timedelta(days=days)

        async with db.execute(
            """SELECT * FROM trades
               WHERE status = 'closed' AND exit_date >= ?
               ORDER BY exit_date DESC""",
            (since.isoformat(),),
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return f"No closed trades in the last {days} days."

        lines = [f"📓 TRADE JOURNAL — last {days}d", "──────────────────────"]
        total_pnl = 0.0
        wins = 0

        for row in rows:
            t = dict(row)
            entry   = t.get("entry_credit") or 0
            exit_p  = t.get("exit_price") or 0
            pnl     = t.get("pnl") or 0
            outcome = t.get("outcome") or "?"
            reason  = t.get("exit_reason") or "?"

            entry_dt = (t.get("entry_date") or "")[:10]
            exit_dt  = (t.get("exit_date") or "")[:10]
            try:
                hold = (datetime.fromisoformat(exit_dt) - datetime.fromisoformat(entry_dt)).days
            except Exception:
                hold = 0

            icon = _OUTCOME_ICON.get(outcome, "?")
            lines.append(f"\n{_leg_header(t)}  {entry_dt} → {exit_dt}  ({hold}d)")
            lines.append(f"  Entry: ${entry:.2f}  Exit: ${exit_p:.2f}  P&L: {_fmt_pnl(pnl)}  {icon} {outcome}  [{reason}]")

            total_pnl += float(pnl)
            if outcome in ("Win", "Assigned"):
                wins += 1

        n   = len(rows)
        pct = wins / n * 100 if n else 0
        lines += [
            "──────────────────────",
            f"{n} trades  Win: {wins}/{n} ({pct:.0f}%)  Total P&L: {_fmt_pnl(total_pnl)}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # /wheel [cycle_id | trade_id]
    # ------------------------------------------------------------------

    async def get_wheel_cycle(self, db, id_arg: str) -> str:
        # Accept either cycle_id or a trade_id whose wheel_cycle_id we look up
        cycle_id = id_arg
        async with db.execute(
            "SELECT wheel_cycle_id FROM trades WHERE trade_id = ?", (id_arg,)
        ) as cur:
            row = await cur.fetchone()
        if row and row["wheel_cycle_id"]:
            cycle_id = row["wheel_cycle_id"]

        async with db.execute(
            "SELECT * FROM wheel_cycles WHERE cycle_id = ?", (cycle_id,)
        ) as cur:
            cycle_row = await cur.fetchone()

        if not cycle_row:
            # No specific cycle — list all cycles for the user to pick from
            return await self._list_wheel_cycles(db)

        cycle = dict(cycle_row)
        async with db.execute(
            "SELECT * FROM trades WHERE wheel_cycle_id = ? ORDER BY entry_date",
            (cycle_id,),
        ) as cur:
            trade_rows = await cur.fetchall()

        lines = [
            f"🔄 WHEEL CYCLE — {cycle['underlying']}",
            "──────────────────────",
            f"Started:  {str(cycle['started_at'])[:10]}",
            f"Status:   {cycle['status'].upper()}",
        ]
        if cycle.get("closed_at"):
            lines.append(f"Closed:   {str(cycle['closed_at'])[:10]}")

        total_credit = 0.0
        total_pnl    = 0.0

        for row in trade_rows:
            t = dict(row)
            legs  = json.loads(t["legs"]) if isinstance(t["legs"], str) else t["legs"]
            short = next((l for l in legs if l.get("action") == "SELL"), None)
            entry  = float(t.get("entry_credit") or 0)
            pnl    = t.get("pnl")
            outcome = t.get("outcome") or "open"
            icon   = _OUTCOME_ICON.get(outcome, "⏳")
            expiry = (short["expiry"][5:] if short else "?")

            leg_str = f"${short['strike']:.0f}{'P' if t['strategy']=='CSP' else 'C'}  {expiry}" if short else "?"
            strat_label = t["strategy"]

            if t["status"] == "open":
                lines.append(f"\n  {strat_label:<14} {leg_str}  Entry: ${entry:.2f}  [open]")
            else:
                lines.append(f"\n  {strat_label:<14} {leg_str}  Entry: ${entry:.2f}  P&L: {_fmt_pnl(pnl)}  {icon}")
                if t.get("exit_reason") == "assignment" and short:
                    net_cost = round(float(short["strike"]) - entry, 2)
                    lines.append(f"    → Assigned — net cost basis ${net_cost:.2f}/sh")

            total_credit += entry
            if t["status"] == "closed" and pnl is not None:
                total_pnl += float(pnl)

        lines += [
            "",
            "──────────────────────",
            f"Credit collected: ${total_credit:.2f}/sh  (${total_credit * 100:.0f})",
        ]
        if total_pnl:
            lines.append(f"Realised P&L:     {_fmt_pnl(total_pnl)}")
        if cycle.get("cycle_return_pct"):
            lines.append(f"Cycle return:     {float(cycle['cycle_return_pct']) * 100:.1f}%")

        return "\n".join(lines)

    async def _list_wheel_cycles(self, db) -> str:
        async with db.execute(
            "SELECT * FROM wheel_cycles ORDER BY started_at DESC LIMIT 10"
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return "No Wheel cycles found. Cycles are created when a CSP fills."

        lines = ["🔄 WHEEL CYCLES", "──────────────────────"]
        for row in rows:
            c = dict(row)
            pnl_str = _fmt_pnl(c.get("total_pnl")) if c.get("total_pnl") is not None else "open"
            status = "✅" if c["status"] == "closed" else "⏳"
            lines.append(
                f"{status} {c['underlying']}  {str(c['started_at'])[:10]}  "
                f"P&L: {pnl_str}  ID: {c['cycle_id'][:8]}..."
            )
        lines.append("\nUse /wheel <cycle_id> for full breakdown.")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # /analyze [dim]
    # ------------------------------------------------------------------

    async def analyze_by_tag(self, db, tag: str = "") -> str:
        async with db.execute(
            "SELECT * FROM trades WHERE status = 'closed' AND outcome IS NOT NULL"
        ) as cur:
            rows = await cur.fetchall()

        if not rows:
            return "No closed trades with outcome data yet."

        trades = [dict(r) for r in rows]
        tag_lower = tag.lower()

        if tag_lower == "ivr":
            groups = {}
            for t in trades:
                k = _ivr_band(t.get("entry_ivr"))
                groups.setdefault(k, []).append(t)
            return _analyze_groups(groups, "📊 WIN RATE BY IVR")

        if tag_lower == "dte":
            groups = {}
            for t in trades:
                k = _dte_band(t.get("entry_dte"))
                groups.setdefault(k, []).append(t)
            return _analyze_groups(groups, "📊 WIN RATE BY DTE")

        if tag_lower == "strategy":
            groups = {}
            for t in trades:
                k = t.get("strategy") or "?"
                groups.setdefault(k, []).append(t)
            return _analyze_groups(groups, "📊 WIN RATE BY STRATEGY")

        if tag_lower == "" or tag_lower == "all":
            sections = []
            for dim, key_fn, header in [
                ("ivr",      lambda t: _ivr_band(t.get("entry_ivr")),          "📊 WIN RATE BY IVR"),
                ("dte",      lambda t: _dte_band(t.get("entry_dte")),           "📊 WIN RATE BY DTE"),
                ("strategy", lambda t: t.get("strategy") or "?",                "📊 WIN RATE BY STRATEGY"),
            ]:
                groups = {}
                for t in trades:
                    k = key_fn(t)
                    groups.setdefault(k, []).append(t)
                sections.append(_analyze_groups(groups, header))
            return "\n\n".join(sections)

        # tag is a rule_tag string
        matching = [
            t for t in trades
            if tag in json.loads(t.get("rule_tags") or "[]")
        ]
        if not matching:
            return f"No closed trades tagged '{tag}'.\n\nAvailable dims: ivr, dte, strategy"
        groups = {}
        for t in matching:
            k = t.get("strategy") or "?"
            groups.setdefault(k, []).append(t)
        return _analyze_groups(groups, f"📊 WIN RATE — tag: {tag}")

    # ------------------------------------------------------------------
    # Monthly auto-report  (scheduled 1st of month, 8am ET)
    # ------------------------------------------------------------------

    async def send_monthly_report(self, telegram_bot) -> None:
        from bot.database import get_db

        now = datetime.now(timezone.utc)
        month_end   = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        month_start = (month_end - timedelta(days=1)).replace(day=1)
        month_label = month_start.strftime("%B %Y")

        async with get_db() as db:
            async with db.execute(
                """SELECT * FROM trades
                   WHERE status = 'closed'
                     AND exit_date >= ? AND exit_date < ?""",
                (month_start.isoformat(), month_end.isoformat()),
            ) as cur:
                rows = await cur.fetchall()

            # Wheel cycle count for the month
            async with db.execute(
                """SELECT COUNT(*) FROM wheel_cycles
                   WHERE started_at >= ? AND started_at < ?""",
                (month_start.isoformat(), month_end.isoformat()),
            ) as cur:
                wc_row = await cur.fetchone()
            wheel_count = wc_row[0] if wc_row else 0

        trades = [dict(r) for r in rows]
        if not trades:
            await telegram_bot.send_alert(
                f"📊 Monthly Report — {month_label}\nNo closed trades this month."
            )
            return

        n       = len(trades)
        wins    = sum(1 for t in trades if t.get("outcome") in ("Win", "Assigned"))
        total   = sum(float(t.get("pnl") or 0) for t in trades)
        win_pct = wins / n * 100 if n else 0

        by_strat: dict = {}
        for t in trades:
            s = t.get("strategy", "?")
            by_strat.setdefault(s, {"count": 0, "pnl": 0.0})
            by_strat[s]["count"] += 1
            by_strat[s]["pnl"]   += float(t.get("pnl") or 0)

        # Avg slippage: difference between expected fill and actual, per trade
        slips = []
        for t in trades:
            ec  = t.get("entry_credit")
            ep  = t.get("exit_price")
            pnl = t.get("pnl")
            if ec is not None and ep is not None and pnl is not None:
                expected_pnl = (float(ec) - float(ep)) * 100
                slips.append(float(pnl) - expected_pnl)
        avg_slip = sum(slips) / len(slips) if slips else 0

        lines = [
            f"📊 MONTHLY REPORT — {month_label}",
            "──────────────────────",
            f"Trades:      {n}  |  Win rate: {wins}/{n} ({win_pct:.0f}%)",
            f"Total P&L:   {_fmt_pnl(total)}",
            f"Avg slippage:{_fmt_pnl(avg_slip)} / trade",
            f"Wheel cycles started: {wheel_count}",
            "",
            "By Strategy",
        ]
        for strat, stats in sorted(by_strat.items()):
            lines.append(
                f"  {strat:<16} {stats['count']:>2} trades  {_fmt_pnl(stats['pnl'])}"
            )

        await telegram_bot.send_alert("\n".join(lines))
