"""
M3: Telegram bot setup and dispatcher.

Security: only responds to allowlisted user IDs from TELEGRAM_ALLOWED_USER_IDS.
All other users are silently ignored — no error response returned.

PRD reference: §5 M3 Telegram Interface.
"""

import logging
from typing import Set

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    filters,
)

from bot.telegram.commands import register_commands
from bot.telegram.notifications import Notifications

logger = logging.getLogger(__name__)


class TelegramBot:
    def __init__(self, token: str, allowed_user_ids: Set[int], config):
        self.token = token
        self.allowed_user_ids = allowed_user_ids
        self.config = config
        self.notifications: Notifications = None   # set after wire()
        self._app: Application = None

        # Modules wired after construction (avoid circular deps)
        self.ibkr = None
        self.risk_engine = None
        self.position_manager = None
        self.execution_engine = None

    def wire(self, ibkr, risk_engine, position_manager, execution_engine) -> None:
        """Wire module dependencies after all are constructed."""
        self.ibkr = ibkr
        self.risk_engine = risk_engine
        self.position_manager = position_manager
        self.execution_engine = execution_engine
        self.notifications = Notifications(self._app.bot if self._app else None, self.allowed_user_ids)

    async def run(self) -> None:
        """Build the application, register handlers, and run until interrupted."""
        self._app = (
            Application.builder()
            .token(self.token)
            .build()
        )

        # Re-init notifications with the real bot instance
        self.notifications = Notifications(self._app.bot, self.allowed_user_ids)

        # Auth filter — silently ignore non-allowlisted users
        auth_filter = filters.User(user_id=list(self.allowed_user_ids))

        # Register all command handlers
        register_commands(self._app, self, auth_filter)

        # Unhandled messages: silently ignore
        logger.info("Telegram bot starting. Allowed user IDs: %s", self.allowed_user_ids)
        await self._app.run_polling(drop_pending_updates=True)

    # ------------------------------------------------------------------
    # Alert helpers (called from other modules)
    # ------------------------------------------------------------------

    async def send_alert(self, message: str) -> None:
        """Send a message to all allowlisted users. Used for system alerts."""
        if self.notifications:
            await self.notifications.broadcast(message)
        else:
            logger.warning("Telegram not ready, dropping alert: %s", message)

    async def send_morning_summary(self) -> None:
        """
        9:30am ET daily morning summary.

        PRD §5 M3 Push Notifications — Morning Summary.
        TODO (Phase 4): compile open positions, Greeks, day's agenda.
        """
        raise NotImplementedError
