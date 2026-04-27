"""
M3: Telegram bot setup and dispatcher.

Security: only responds to allowlisted user IDs from TELEGRAM_ALLOWED_USER_IDS.
All other users are silently ignored — no error response returned.

PRD reference: §5 M3 Telegram Interface.
"""

import asyncio
import logging
from typing import Optional, Set

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
        self.notifications: Notifications = None
        self._app: Application = None
        self._stop_event: Optional[asyncio.Event] = None

        # Modules wired after construction (avoid circular deps)
        self.ibkr = None
        self.risk_engine = None
        self.position_manager = None
        self.execution_engine = None
        self.premium_scanner = None
        self.momentum_scanner = None

    def wire(self, ibkr, risk_engine, position_manager, execution_engine) -> None:
        """
        Wire module dependencies after all are constructed.

        Notifications is NOT initialised here — self._app is not built yet
        (it's created in run()). Attempting to access self._app.bot here
        produces a Notifications with bot=None, which silently drops every
        alert during startup. Notifications is initialised once in run().

        execution_engine.on_notify is set to self.send_alert, which checks
        self.notifications at call time, so it is safe to assign here even
        though notifications is not yet initialised.
        """
        self.ibkr = ibkr
        self.risk_engine = risk_engine
        self.position_manager = position_manager
        self.execution_engine = execution_engine
        execution_engine.on_notify = self.send_alert
        risk_engine.on_notify = self.send_alert
        position_manager.on_alert = self.send_alert
        position_manager.set_execution_engine(execution_engine)

    def wire_scanners(self, premium_scanner, momentum_scanner) -> None:
        """
        Store scanner references so Telegram commands can invoke them on demand.

        Called by build_scheduler() after scanner instances are created,
        so /scan has a handle to call premium_scanner.run() directly.

        Also wires the on_proposal callback so the scanner can broadcast
        trade cards without holding a direct reference to TelegramBot.
        self.send_alert is safe to capture here — it checks self.notifications
        at call time, not at wiring time.
        """
        self.premium_scanner = premium_scanner
        self.momentum_scanner = momentum_scanner
        premium_scanner.on_proposal = self.send_alert

    async def run(self) -> None:
        """
        Build the application, register handlers, and run until stop() is called.

        Uses the low-level async API instead of run_polling() because run_polling()
        is a synchronous method that calls loop.run_until_complete() internally —
        calling it from within an already-running event loop raises RuntimeError.
        """
        self._stop_event = asyncio.Event()

        self._app = (
            Application.builder()
            .token(self.token)
            .build()
        )

        # Notifications created here, after self._app is available.
        self.notifications = Notifications(self._app.bot, self.allowed_user_ids)

        # Auth filter — silently ignore non-allowlisted users
        auth_filter = filters.User(user_id=list(self.allowed_user_ids))

        # Register all command handlers
        register_commands(self._app, self, auth_filter)

        logger.info("Telegram bot starting. Allowed user IDs: %s", self.allowed_user_ids)

        async with self._app:
            await self._app.updater.start_polling(drop_pending_updates=True)
            await self._app.start()
            await self._stop_event.wait()
            await self._app.updater.stop()
            await self._app.stop()

    async def stop(self) -> None:
        """Signal run() to shut down the Telegram polling loop."""
        if self._stop_event:
            self._stop_event.set()

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
