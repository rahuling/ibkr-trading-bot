"""
M1: Premium-selling scanner (9:45am and 3:00pm ET).

Scans Core watchlist for CSP candidates and Tactical watchlist for
Bull Put Spread candidates. Feeds M2 (TradeBuilder).

PRD reference: §5 M1 Market Scanner — Scanner Logic steps 1–10.
"""

import asyncio
import logging
import math
from dataclasses import dataclass
from typing import List, Optional

from bot.database import get_db
from bot.scanner.base import compute_ivr, get_current_iv, has_earnings_soon

logger = logging.getLogger(__name__)


@dataclass
class ScanCandidate:
    ticker: str
    strategy: str           # "CSP" or "BullPutSpread"
    current_price: float
    current_iv: float
    ivr: float
    suggested_dte_min: int
    suggested_dte_max: int
    bucket: str             # "Core" or "Tactical"


class PremiumScanner:
    """
    Runs the 9:45am and 3:00pm premium-selling scan.

    Returns top N candidates ranked by IVR descending, filtered
    by earnings blackout, existing positions, and bucket capacity.

    on_proposal is wired by TelegramBot.wire_scanners after both objects
    are constructed. It's an async callable(trade_card: str) that broadcasts
    the trade card to all allowed users.
    """

    def __init__(self, config, ibkr):
        self.config = config
        self.ibkr = ibkr
        self.on_proposal = None  # set by TelegramBot.wire_scanners

    async def run(self) -> List[ScanCandidate]:
        """
        Execute the full premium-selling scan.

        Steps (PRD §5 M1 Scanner Logic):
          1–7: per-ticker eligibility + IV/IVR filters (in _scan_bucket)
          8. Rank by IVR descending
          9. Tag Core → CSP, Tactical → BullPutSpread
         10. Build proposals and send to Telegram
        """
        cfg = self.config

        core_candidates = await self._scan_bucket(
            tickers=cfg.scanner.watchlist.core,
            strategy="CSP",
            bucket="Core",
            min_ivr=cfg.risk.min_ivr_core,
        )
        tactical_candidates = await self._scan_bucket(
            tickers=cfg.scanner.watchlist.tactical,
            strategy="BullPutSpread",
            bucket="Tactical",
            min_ivr=cfg.risk.min_ivr_tactical,
        )

        all_candidates = core_candidates + tactical_candidates
        all_candidates.sort(key=lambda c: c.ivr, reverse=True)
        top_n = all_candidates[: cfg.scanner.top_n_candidates]

        if not top_n:
            logger.info("Premium scan: no candidates passed all filters")
            return []

        logger.info(
            "Premium scan: %d/%d candidates selected for proposals",
            len(top_n), len(all_candidates),
        )

        await self._build_and_send_proposals(top_n)
        return top_n

    async def _scan_bucket(
        self,
        tickers: List[str],
        strategy: str,
        bucket: str,
        min_ivr: int,
    ) -> List[ScanCandidate]:
        """Scan one bucket (Core or Tactical) and return filtered candidates."""
        from ib_async import Stock

        candidates = []
        async with get_db() as db:
            for ticker in tickers:
                try:
                    if not await self._is_eligible(ticker, bucket, db):
                        continue

                    iv = await get_current_iv(self.ibkr, ticker)
                    if iv is None:
                        logger.debug("%s: no IV data — skipping", ticker)
                        continue

                    ivr = await compute_ivr(db, ticker, iv)
                    if ivr is None:
                        logger.debug("%s: insufficient IV history for IVR — skipping", ticker)
                        continue
                    if ivr < min_ivr:
                        logger.debug("%s: IVR %.1f below minimum %d — skipping", ticker, ivr, min_ivr)
                        continue

                    # Fetch current price
                    stock = Stock(ticker, "SMART", "USD")
                    [qualified] = await self.ibkr.ib.qualifyContractsAsync(stock)
                    snaps = await self.ibkr.ib.reqTickersAsync(qualified)
                    if not snaps:
                        logger.debug("%s: no market data snapshot — skipping", ticker)
                        continue
                    snap = snaps[0]
                    # Prefer last trade; fall back to close (after hours). NaN is falsy after isnan check.
                    raw_price = snap.last if (snap.last and not math.isnan(snap.last)) else snap.close
                    if not raw_price or math.isnan(raw_price) or raw_price <= 0:
                        logger.debug("%s: no price data — skipping", ticker)
                        continue
                    price = raw_price

                    dte_cfg = (
                        self.config.trading.csp
                        if strategy == "CSP"
                        else self.config.trading.spread
                    )
                    candidates.append(ScanCandidate(
                        ticker=ticker,
                        strategy=strategy,
                        current_price=float(price),
                        current_iv=iv,
                        ivr=ivr,
                        suggested_dte_min=dte_cfg.dte_min,
                        suggested_dte_max=dte_cfg.dte_max,
                        bucket=bucket,
                    ))
                    logger.info(
                        "%s: passed filters — price=%.2f IV=%.2f IVR=%.1f strategy=%s",
                        ticker, price, iv, ivr, strategy,
                    )

                except Exception as exc:
                    logger.error("_scan_bucket error on %s: %s", ticker, exc, exc_info=True)

        return candidates

    async def _is_eligible(self, ticker: str, bucket: str, db) -> bool:
        """
        Check all disqualifying conditions for a ticker.
        Returns False if any condition disqualifies it.
        """
        cfg = self.config.risk

        # 1. Earnings blackout
        if await has_earnings_soon(
            self.ibkr, ticker,
            cfg.earnings_blackout_pre_days,
            cfg.earnings_blackout_post_days,
        ):
            return False

        # 2. Already have an open position on this underlying
        async with db.execute(
            "SELECT COUNT(*) FROM trades WHERE underlying = ? AND status = 'open'",
            (ticker,),
        ) as cursor:
            row = await cursor.fetchone()
            if row and row[0] > 0:
                logger.debug("%s: open position exists — skipping", ticker)
                return False

        # 3. Simplified bucket capacity: max 3 simultaneous positions per bucket
        # (since max_position_pct_of_bucket = 0.33, meaning 3 fills the bucket)
        # Full capital-aware check is implemented in Phase 4 risk engine.
        async with db.execute(
            "SELECT COUNT(*) FROM trades WHERE bucket = ? AND status = 'open'",
            (bucket,),
        ) as cursor:
            row = await cursor.fetchone()
            open_count = row[0] if row else 0
            if open_count >= 3:
                logger.debug("%s: %s bucket at capacity (%d open) — skipping", ticker, bucket, open_count)
                return False

        return True

    async def _build_and_send_proposals(self, candidates: List[ScanCandidate]) -> None:
        """Build trade proposals, save to DB, and broadcast via Telegram."""
        import json
        import uuid
        from datetime import datetime, timedelta, timezone

        from bot.builder.csp import build_csp_proposal, format_csp_trade_card
        from bot.builder.spread import build_spread_proposal, format_spread_trade_card

        async with get_db() as db:
            for candidate in candidates:
                try:
                    if candidate.strategy == "CSP":
                        proposal = await build_csp_proposal(self.config, self.ibkr, candidate)
                        if not proposal:
                            logger.info("No CSP proposal built for %s", candidate.ticker)
                            continue
                        proposal_id = uuid.uuid4().hex[:6].upper()
                        trade_card = format_csp_trade_card(proposal, proposal_id)
                        trade_card_json = json.dumps(proposal.__dict__)
                    else:
                        proposal = await build_spread_proposal(self.config, self.ibkr, candidate)
                        if not proposal:
                            logger.info("No spread proposal built for %s", candidate.ticker)
                            continue
                        proposal_id = uuid.uuid4().hex[:6].upper()
                        trade_card = format_spread_trade_card(proposal, proposal_id)
                        trade_card_json = json.dumps(proposal.__dict__)

                    now = datetime.now(timezone.utc)
                    expires_at = now + timedelta(minutes=self.config.execution.proposal_ttl_minutes)

                    await db.execute(
                        """INSERT INTO proposals
                           (proposal_id, underlying, strategy, trade_card_json,
                            status, created_at, expires_at)
                           VALUES (?, ?, ?, ?, 'pending', ?, ?)""",
                        (
                            proposal_id,
                            candidate.ticker,
                            candidate.strategy,
                            trade_card_json,
                            now.isoformat(),
                            expires_at.isoformat(),
                        ),
                    )
                    await db.commit()
                    logger.info(
                        "Proposal %s saved: %s %s",
                        proposal_id, candidate.ticker, candidate.strategy,
                    )

                    if self.on_proposal:
                        await self.on_proposal(trade_card)

                except Exception as exc:
                    logger.error(
                        "Failed to build/send proposal for %s: %s",
                        candidate.ticker, exc, exc_info=True,
                    )
