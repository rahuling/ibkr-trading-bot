"""
M3: Push notification helpers.

All outbound Telegram messages that aren't command responses go through here.

PRD reference: §5 M3 Telegram Interface — Push Notifications table.
"""

import logging
from typing import Set

logger = logging.getLogger(__name__)


class Notifications:
    def __init__(self, bot, allowed_user_ids: Set[int]):
        self.bot = bot
        self.allowed_user_ids = allowed_user_ids

    async def broadcast(self, text: str, parse_mode: str = None) -> None:
        """Send a message to all allowlisted users."""
        if not self.bot:
            logger.warning("Bot not ready, dropping notification: %s", text[:80])
            return
        for user_id in self.allowed_user_ids:
            try:
                await self.bot.send_message(
                    chat_id=user_id,
                    text=text,
                    parse_mode=parse_mode,
                )
            except Exception as exc:
                logger.error("Failed to send notification to %s: %s", user_id, exc)

    async def trade_proposal(self, trade_card: str) -> None:
        """📋 New trade proposal."""
        await self.broadcast(trade_card)

    async def fill_confirmation(self, underlying: str, strategy: str,
                                 expected: float, actual: float) -> None:
        """✅ Order filled."""
        slippage = actual - expected
        slip_str = f"+${slippage:.2f}" if slippage > 0 else f"-${abs(slippage):.2f}"
        await self.broadcast(
            f"✅ FILLED: {underlying} {strategy}\n"
            f"Expected: ${expected:.2f}  |  Actual: ${actual:.2f}  |  Slippage: {slip_str}"
        )

    async def strike_tested(self, underlying: str, strike: float, current: float) -> None:
        """⚠️ Underlying within 2% of short strike."""
        pct = abs(current - strike) / strike * 100
        await self.broadcast(
            f"⚠️ STRIKE TESTED: {underlying} at ${current:.2f} "
            f"({pct:.1f}% from ${strike:.0f} strike)"
        )

    async def profit_target_hit(self, underlying: str, strategy: str, pnl: float) -> None:
        """📈 Profit target reached."""
        await self.broadcast(
            f"📈 PROFIT TARGET: {underlying} {strategy} — ${pnl:.0f} profit. Consider closing."
        )

    async def risk_limit_breached(self, limit_type: str, current: float, limit: float) -> None:
        """🚨 Risk limit hit."""
        await self.broadcast(
            f"🚨 RISK LIMIT: {limit_type} — current: ${current:,.0f} / limit: ${limit:,.0f}\n"
            f"New trades paused. Use /resume after review."
        )

    async def assignment_alert(self, underlying: str, shares: int, cost_basis: float) -> None:
        """📉 Assignment."""
        await self.broadcast(
            f"📉 ASSIGNED: {shares} shares of {underlying} received.\n"
            f"Net cost basis: ${cost_basis:.2f}/share\n"
            f"Covered Call proposal queued for morning scan."
        )

    async def leap_stop_triggered(self, underlying: str, stop_price: float,
                                   current_price: float) -> None:
        """⛔ LEAP stop loss triggered."""
        await self.broadcast(
            f"⛔ STOP LOSS: {underlying} underlying at ${current_price:.2f} "
            f"(stop: ${stop_price:.2f}). Submitting close order."
        )

    async def leap_target_hit(self, underlying: str, target_price: float,
                               current_price: float) -> None:
        """📈 LEAP profit target hit."""
        await self.broadcast(
            f"📈 PROFIT TARGET: {underlying} at ${current_price:.2f} "
            f"(target: ${target_price:.2f}). Submitting close order."
        )

    async def proposal_expired(self, proposal_id: str, underlying: str, strategy: str) -> None:
        """⏰ Proposal TTL elapsed."""
        await self.broadcast(
            f"⏰ Proposal #{proposal_id} ({underlying} {strategy}) expired.\n"
            f"Run /scan for fresh proposals."
        )

    async def pdt_warning(self, portfolio_value: float, threshold: float) -> None:
        """⚠️ Approaching PDT threshold."""
        await self.broadcast(
            f"⚠️ PDT WARNING: Portfolio at ${portfolio_value:,.0f} "
            f"(approaching ${threshold:,.0f} PDT threshold).\n"
            f"Review open positions."
        )
