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
from typing import Optional

from ib_async import IB

logger = logging.getLogger(__name__)

_BACKOFF_STEPS = [5, 10, 30, 60]


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

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect to IB Gateway. Raises on failure (caller handles retry)."""
        logger.info("Connecting to IB Gateway at %s:%s (client_id=%s)",
                    self.host, self.port, self.client_id)
        await self.ib.connectAsync(self.host, self.port, clientId=self.client_id)
        self.ib.disconnectedEvent += self._on_disconnect
        logger.info("Connected to IB Gateway. Account: %s", self.ib.client.accounts)

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
                    await self._on_alert(
                        f"⚠️ IB Gateway disconnected. Attempting reconnect..."
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
                return float(av.value)
        return None

    def get_account_id(self) -> Optional[str]:
        accounts = self.ib.managedAccounts()
        return accounts[0] if accounts else None


def create_ibkr_connection(on_alert=None) -> IBKRConnection:
    """Create an IBKRConnection from environment variables."""
    return IBKRConnection(
        host=os.getenv("IB_GATEWAY_HOST", "127.0.0.1"),
        port=int(os.getenv("IB_GATEWAY_PORT", "4002")),
        client_id=int(os.getenv("IB_CLIENT_ID", "1")),
        on_alert=on_alert,
    )
