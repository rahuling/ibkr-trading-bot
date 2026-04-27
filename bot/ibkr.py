"""
IB Gateway connection manager.

Maintains a persistent ib_async connection with automatic reconnect
and exponential backoff. All other modules receive the IB instance
from here — never create their own connections.

Reconnect sequence:
    5s → 10s → 30s → 60s → Telegram alert every 60s until restored
"""

import asyncio
import logging
import os
from datetime import datetime
from datetime import time as dtime
from typing import Optional

import pytz
from ib_async import IB

logger = logging.getLogger(__name__)

_BACKOFF_STEPS = [5, 10, 30, 60]
_ET = pytz.timezone("America/New_York")


def _is_nightly_restart_window() -> bool:
    """Return True during 11:58pm–12:05am ET — the IB Gateway nightly restart window."""
    now = datetime.now(_ET).time()
    return now >= dtime(23, 58) or now <= dtime(0, 5)


class IBKRConnection:
    """
    Wrapper around ib_async.IB that handles connection lifecycle,
    reconnect with backoff, and startup state reconciliation.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        on_alert=None,   # async callable(msg: str) — sends Telegram alert
    ):
        self.host = host
        self.port = port
        self.client_id = client_id
        self._on_alert = on_alert
        self.ib = IB()
        self._reconnect_task: Optional[asyncio.Task] = None
        # Register the disconnect handler exactly once on the IB object that
        # persists across reconnects. Never register again inside connect() —
        # doing so accumulates duplicate handlers, spawning two reconnect loops
        # on each subsequent disconnect.
        self.ib.disconnectedEvent += self._on_disconnect

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to IB Gateway. Raises on failure (caller handles retry)."""
        logger.info("Connecting to IB Gateway at %s:%s (client_id=%s)",
                    self.host, self.port, self.client_id)
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        logger.info("Connected to IB Gateway. Account: %s", self.ib.managedAccounts())

    async def disconnect(self) -> None:
        self.ib.disconnect()

    @property
    def is_connected(self) -> bool:
        return self.ib.isConnected()

    # ------------------------------------------------------------------
    # Reconnect logic
    # ------------------------------------------------------------------

    def _on_disconnect(self) -> None:
        logger.warning("IB Gateway disconnected — starting reconnect loop")
        # Cancel any in-flight reconnect loop before starting a new one.
        # Without this, a second disconnect event while a loop is already
        # running would orphan the old task and create two competing loops.
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        """Exponential backoff reconnect. Alerts via Telegram after first failure."""
        attempt = 0
        while not self.ib.isConnected():
            delay = _BACKOFF_STEPS[min(attempt, len(_BACKOFF_STEPS) - 1)]
            logger.info("Reconnect attempt %s in %ss...", attempt + 1, delay)
            await asyncio.sleep(delay)

            try:
                await self.connect()
                logger.info("Reconnected to IB Gateway on attempt %s", attempt + 1)
                # TODO (Phase 4): trigger state reconciliation after reconnect
                return
            except Exception as exc:
                logger.error("Reconnect attempt %s failed: %s", attempt + 1, exc)
                if attempt == 0 and self._on_alert:
                    if _is_nightly_restart_window():
                        logger.info("Disconnect during nightly restart window — suppressing Telegram alert")
                    else:
                        await self._on_alert(
                            "⚠️ IB Gateway disconnected. Attempting reconnect..."
                        )
                elif attempt >= len(_BACKOFF_STEPS) - 1 and self._on_alert:
                    await self._on_alert(
                        f"🚨 IB Gateway still disconnected after {attempt + 1} attempts."
                    )

            attempt += 1

    # ------------------------------------------------------------------
    # Account helpers
    # ------------------------------------------------------------------

    def get_net_liquidation(self) -> Optional[float]:
        """Return current Net Liquidation Value from IBKR account data."""
        for av in self.ib.accountValues():
            if av.tag == "NetLiquidation" and av.currency == "USD":
                try:
                    return float(av.value)
                except (ValueError, TypeError):
                    logger.error("Invalid NetLiquidation value from IBKR: %r", av.value)
                    return None
        return None

    def get_account_id(self) -> Optional[str]:
        accounts = self.ib.managedAccounts()
        return accounts[0] if accounts else None


def create_ibkr_connection(on_alert=None) -> IBKRConnection:
    """Create an IBKRConnection from environment variables."""
    try:
        port = int(os.getenv("IB_GATEWAY_PORT", "4002"))
        client_id = int(os.getenv("IB_CLIENT_ID", "1"))
    except ValueError as exc:
        raise ValueError(
            f"Invalid IB_GATEWAY_PORT or IB_CLIENT_ID in environment: {exc}"
        ) from exc
    return IBKRConnection(
        host=os.getenv("IB_GATEWAY_HOST", "127.0.0.1"),
        port=port,
        client_id=client_id,
        on_alert=on_alert,
    )
