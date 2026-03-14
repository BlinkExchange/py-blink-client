"""WebSocket clients for the Blink market, price, and user channels."""
from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Set

from .constants import WS_MARKET_PATH, WS_USER_PATH
from .exceptions import BlinkWebSocketError
from .types import ApiCreds

logger = logging.getLogger(__name__)

# Try to import the WS_PRICE_PATH constant; define fallback if not present.
try:
    from .constants import WS_PRICE_PATH
except ImportError:
    WS_PRICE_PATH = "/ws/price"

# Callback type alias
Callback = Callable[[Dict[str, Any]], None]


def _strip_0x(hex_str: str) -> str:
    """Return lowercase hex string with 0x prefix stripped."""
    s = hex_str.lower()
    if s.startswith("0x"):
        return s[2:]
    return s


# ---------------------------------------------------------------------------
# Base WebSocket
# ---------------------------------------------------------------------------


class _BaseWs:
    """Shared WebSocket machinery: connect, reconnect, subscribe, dispatch."""

    def __init__(self, base_url: str, path: str) -> None:
        # Normalise URL: strip trailing slash, convert http(s) to ws(s)
        url = base_url.rstrip("/")
        if url.startswith("https://"):
            url = "wss://" + url[len("https://"):]
        elif url.startswith("http://"):
            url = "ws://" + url[len("http://"):]
        self._ws_url = url + path

        self._running = False
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws: Any = None  # Reference to the live websocket for sending

        # Subscriptions -- both sets hold normalised asset IDs
        self._subscribed: Set[str] = set()
        self._pending_subs: Set[str] = set()

        # Reconnection
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0

        # Generic callbacks
        self.on_connected: Optional[Callable[[], None]] = None
        self.on_disconnected: Optional[Callable[[], None]] = None
        self.on_error: Optional[Callable[[Exception], None]] = None

    # -- public helpers --

    @property
    def connected(self) -> bool:
        """Whether the WebSocket is currently connected."""
        return self._connected

    # -- subscription management (overridden in subclasses that need normalisation) --

    def _normalise_asset_id(self, asset_id: str) -> str:
        """Normalise an asset ID before storing/sending.  Default: identity."""
        return asset_id

    def subscribe(self, asset_ids: List[str]) -> None:
        """Queue asset IDs for subscription.

        If already connected the subscribe message is sent immediately.
        Otherwise the IDs are queued and sent on next (re)connect.
        """
        normalised = [self._normalise_asset_id(a) for a in asset_ids]
        self._pending_subs.update(normalised)
        # If we are already connected, flush immediately
        if self._connected and self._ws is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda ids=list(normalised): asyncio.ensure_future(self._send_subscribe(ids))  # type: ignore[arg-type]
            )

    def unsubscribe(self, asset_ids: List[str]) -> None:
        """Unsubscribe from asset IDs.

        Sends an unsubscribe message to the server if connected and removes
        them from the local subscription tracking sets.
        """
        normalised = [self._normalise_asset_id(a) for a in asset_ids]
        for tid in normalised:
            self._subscribed.discard(tid)
            self._pending_subs.discard(tid)
        # Send unsubscribe to server if connected
        if self._connected and self._ws is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda ids=list(normalised): asyncio.ensure_future(self._send_unsubscribe(ids))  # type: ignore[arg-type]
            )

    def start(self) -> None:
        """Start the WebSocket listener in a background daemon thread."""
        if self._running:
            logger.warning("%s already running", self.__class__.__name__)
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True, name=self.__class__.__name__)
        self._thread.start()
        logger.info("%s started, connecting to %s", self.__class__.__name__, self._ws_url)

    def stop(self) -> None:
        """Stop the WebSocket listener and close the connection cleanly.

        Signals the event loop to shut down, closes the WS connection,
        and waits for the background thread to exit cleanly.
        """
        logger.info("Stopping %s", self.__class__.__name__)
        self._running = False

        if self._loop and self._loop.is_running():
            # Schedule the close coroutine on the loop's own thread.
            # This closes the WS (unblocking the read loop) and waits
            # for all pending tasks to complete before stopping.
            try:
                future = asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop)
                future.result(timeout=5)
            except (asyncio.CancelledError, RuntimeError):
                pass  # Loop already stopped or coroutine cancelled — expected
            except Exception as e:
                logger.debug("Shutdown coroutine non-fatal error: %s", e)

        if self._thread:
            self._thread.join(timeout=5)
        self._ws = None
        self._connected = False
        logger.info("%s stopped", self.__class__.__name__)

    async def _shutdown(self) -> None:
        """Async shutdown: close WS, cancel pending tasks, let the read loop exit.

        Runs on the event loop's own thread so we can safely await coroutines.
        """
        # Close the websocket (this unblocks `async for raw_msg in ws:` in _connect_loop)
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

        # Cancel all pending tasks EXCEPT the current one (shutdown itself)
        current = asyncio.current_task()
        for task in asyncio.all_tasks(self._loop):
            if task is not current and not task.done():
                task.cancel()

        # Give cancelled tasks a moment to clean up
        await asyncio.sleep(0.1)

    def wait_for_connection(self, timeout: float = 10.0) -> bool:
        """Block until connected or timeout."""
        deadline = time.monotonic() + timeout
        while not self._connected and time.monotonic() < deadline:
            time.sleep(0.1)
        return self._connected

    # -- internals --

    def _run(self) -> None:
        """Thread target: run the async event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._connect_loop())
        except asyncio.CancelledError:
            # Clean cancellation from stop()
            pass
        except Exception:
            if self._running:
                logger.exception("Event loop crashed")
        finally:
            # Cancel any remaining tasks before closing
            try:
                pending = [t for t in asyncio.all_tasks(self._loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    self._loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            self._loop.close()

    async def _connect_loop(self) -> None:
        """Reconnection loop."""
        try:
            import websockets
            import websockets.asyncio.client as ws_client
        except ImportError as exc:
            raise BlinkWebSocketError(
                "websockets library required: pip install websockets"
            ) from exc

        while self._running:
            try:
                async with ws_client.connect(
                    self._ws_url,
                    ping_interval=30,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    self._connected = True
                    self._reconnect_delay = 1.0
                    logger.info("Connected to %s", self._ws_url)

                    await self._on_open(ws)

                    if self.on_connected:
                        try:
                            self.on_connected()
                        except Exception:
                            logger.exception("on_connected callback error")

                    # Read loop
                    async for raw_msg in ws:
                        try:
                            data = json.loads(raw_msg)
                            self._dispatch(data)
                        except json.JSONDecodeError:
                            logger.warning("Invalid JSON: %s", str(raw_msg)[:100])
                        except Exception:
                            logger.exception("Message handling error")

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning("WebSocket error: %s", exc)
                if self.on_error:
                    try:
                        self.on_error(exc)
                    except Exception:
                        pass

            self._ws = None
            self._connected = False
            if self.on_disconnected:
                try:
                    self.on_disconnected()
                except Exception:
                    pass

            if self._running:
                jitter = random.uniform(0, self._reconnect_delay * 0.5)
                logger.info("Reconnecting in %.1fs (jitter=%.1fs)...", self._reconnect_delay, jitter)
                await asyncio.sleep(self._reconnect_delay + jitter)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )

    async def _send_subscribe(self, asset_ids: List[str]) -> None:
        """Send a subscribe message over the live websocket."""
        if self._ws is not None and asset_ids:
            msg = json.dumps({"type": "subscribe", "assets": asset_ids})
            try:
                await self._ws.send(msg)
                logger.debug("Sent subscribe for %d assets", len(asset_ids))
            except Exception:
                logger.warning("Failed to send subscribe message")

    async def _send_unsubscribe(self, asset_ids: List[str]) -> None:
        """Send an unsubscribe message over the live websocket."""
        if self._ws is not None and asset_ids:
            msg = json.dumps({"type": "unsubscribe", "assets": asset_ids})
            try:
                await self._ws.send(msg)
                logger.debug("Sent unsubscribe for %d assets", len(asset_ids))
            except Exception:
                logger.warning("Failed to send unsubscribe message")

    async def _on_open(self, ws: Any) -> None:
        """Called after WS handshake. Override for auth / subscribe.

        Default behaviour: merge pending + previously subscribed (for reconnect)
        and send a subscribe message for all of them.
        """
        all_assets = self._pending_subs | self._subscribed
        if all_assets:
            msg = json.dumps({"type": "subscribe", "assets": list(all_assets)})
            await ws.send(msg)
            logger.debug("Sent subscribe for %d assets (including re-subscriptions)", len(all_assets))

    def _dispatch(self, data: Dict[str, Any]) -> None:
        """Route an incoming message to the appropriate callback."""
        pass  # Overridden by subclasses


# ---------------------------------------------------------------------------
# Market WebSocket (/ws/market)
# ---------------------------------------------------------------------------


class BlinkMarketWs(_BaseWs):
    """Public market data WebSocket.

    Connects to ``/ws/market`` and dispatches orderbook snapshots,
    trades, and market-status events.

    asset_id values are lowercase hex without a ``0x`` prefix.

    Callbacks (set as attributes):
        on_snapshot(data)
        on_delta(data)
        on_trade(data)
        on_best_prices(data)
        on_market_created(data)
        on_market_status(data)
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url, WS_MARKET_PATH)

        # Event callbacks
        self.on_snapshot: Optional[Callback] = None
        self.on_delta: Optional[Callback] = None
        self.on_trade: Optional[Callback] = None
        self.on_best_prices: Optional[Callback] = None
        self.on_market_created: Optional[Callback] = None
        self.on_market_status: Optional[Callback] = None

    def _normalise_asset_id(self, asset_id: str) -> str:
        """Strip 0x prefix and lowercase for market channel."""
        return _strip_0x(asset_id)

    def subscribe_all(self) -> None:
        """Subscribe to all markets using the wildcard."""
        self.subscribe(["*"])

    def resync(self, asset_id: str) -> None:
        """Request a full orderbook snapshot for gap recovery.

        Use this when you detect a sequence gap in orderbook deltas.
        The server responds with an ``orderbook_snapshot`` message.
        """
        normalised = self._normalise_asset_id(asset_id)
        if self._connected and self._ws is not None and self._loop is not None:
            self._loop.call_soon_threadsafe(
                lambda aid=normalised: asyncio.ensure_future(self._send_resync(aid))  # type: ignore[arg-type]
            )
        else:
            logger.warning("Cannot resync: not connected")

    async def _send_resync(self, asset_id: str) -> None:
        """Send a resync request over the live websocket."""
        if self._ws is not None:
            msg = json.dumps({"type": "resync", "asset_id": asset_id})
            try:
                await self._ws.send(msg)
                logger.debug("Sent resync for asset %s", asset_id)
            except Exception:
                logger.warning("Failed to send resync message")

    def _dispatch(self, data: Dict[str, Any]) -> None:
        event_type = data.get("type", "")

        try:
            if event_type == "orderbook_snapshot":
                if self.on_snapshot:
                    self.on_snapshot(data)

            elif event_type == "orderbook_delta":
                if self.on_delta:
                    self.on_delta(data)

            elif event_type == "trade":
                if self.on_trade:
                    self.on_trade(data)

            elif event_type == "best_prices":
                if self.on_best_prices:
                    self.on_best_prices(data)

            elif event_type == "market_created":
                if self.on_market_created:
                    self.on_market_created(data)

            elif event_type == "market_status":
                if self.on_market_status:
                    self.on_market_status(data)

            elif event_type == "subscribed":
                assets = data.get("assets", [])
                self._subscribed.update(assets)
                # Remove from pending now that they are confirmed
                self._pending_subs -= set(assets)
                logger.debug("Subscription confirmed: %d assets", len(assets))

            elif event_type == "unsubscribed":
                for a in data.get("assets", []):
                    self._subscribed.discard(a)

            elif event_type == "pong":
                pass

            elif event_type == "error":
                logger.warning("Market WS error: %s", data.get("message", data))

            else:
                logger.debug("Unhandled market event: %s", event_type)
        except Exception:
            logger.exception("Market WS callback error for event '%s'", event_type)


# ---------------------------------------------------------------------------
# Price WebSocket (/ws/price)
# ---------------------------------------------------------------------------


class BlinkPriceWs(_BaseWs):
    """Public price tick WebSocket.

    Connects to ``/ws/price`` and dispatches underlying price ticks.

    Notes:
      - The server sends a ``connected`` message on connect.
      - Subscribe uses **symbols** (e.g. ``"AAPLX"``), not token IDs.

    Callbacks (set as attributes):
        on_price_tick(data)
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(base_url, WS_PRICE_PATH)

        # Event callbacks
        self.on_price_tick: Optional[Callback] = None

    def _normalise_asset_id(self, asset_id: str) -> str:
        """Price channel uses symbols as-is (no hex normalisation)."""
        return asset_id

    def _dispatch(self, data: Dict[str, Any]) -> None:
        event_type = data.get("type", "")

        try:
            if event_type == "price_tick":
                if self.on_price_tick:
                    self.on_price_tick(data)

            elif event_type == "subscribed":
                assets = data.get("assets", [])
                self._subscribed.update(assets)
                self._pending_subs -= set(assets)
                logger.debug("Price subscription confirmed: %d assets", len(assets))

            elif event_type in ("connected", "pong"):
                pass

            elif event_type == "error":
                logger.warning("Price WS error: %s", data.get("message", data))

            else:
                logger.debug("Unhandled price event: %s", event_type)
        except Exception:
            logger.exception("Price WS callback error for event '%s'", event_type)


# ---------------------------------------------------------------------------
# User WebSocket (/ws/user) -- authenticated
# ---------------------------------------------------------------------------


class BlinkUserWs(_BaseWs):
    """Authenticated user event WebSocket.

    Connects to ``/ws/user``, authenticates with API credentials, and
    dispatches per-user events (fills, cancellations, positions, PnL).

    Callbacks (set as attributes):
        on_order_accepted(data)
        on_order_rejected(data)
        on_order_fill(data)
        on_order_cancelled(data)
        on_settlement(data)
        on_position_update(data)
        on_redeemable_position(data)
        on_pnl_update(data)
        on_wallet_status(data)
        on_balance_update(data)
        on_orders_snapshot(data)
        on_positions_snapshot(data)
        on_authenticated()
        on_auth_error(data)
    """

    def __init__(self, base_url: str, creds: ApiCreds) -> None:
        super().__init__(base_url, WS_USER_PATH)
        self._creds = creds
        self._authenticated = False

        # Event callbacks
        self.on_order_accepted: Optional[Callback] = None
        self.on_order_rejected: Optional[Callback] = None
        self.on_order_fill: Optional[Callback] = None
        self.on_order_cancelled: Optional[Callback] = None
        self.on_settlement: Optional[Callback] = None
        self.on_position_update: Optional[Callback] = None
        self.on_redeemable_position: Optional[Callback] = None
        self.on_pnl_update: Optional[Callback] = None
        self.on_wallet_status: Optional[Callback] = None
        self.on_balance_update: Optional[Callback] = None
        self.on_orders_snapshot: Optional[Callback] = None
        self.on_positions_snapshot: Optional[Callback] = None
        self.on_authenticated: Optional[Callable[[], None]] = None
        self.on_auth_error: Optional[Callback] = None
        self.on_activity_created: Optional[Callback] = None

    @property
    def authenticated(self) -> bool:
        return self._authenticated

    def wait_for_auth(self, timeout: float = 10.0) -> bool:
        """Block until authenticated or timeout."""
        deadline = time.monotonic() + timeout
        while not self._authenticated and time.monotonic() < deadline:
            time.sleep(0.1)
        return self._authenticated

    async def _on_open(self, ws: Any) -> None:
        """Send the auth envelope on connect."""
        # reset so wait_for_auth() doesn't return stale True after reconnect
        self._authenticated = False

        auth_msg = json.dumps({
            "type": "user",
            "auth": {
                "api_key": self._creds.api_key,
                "secret": self._creds.secret,
                "passphrase": self._creds.passphrase,
            },
        })
        await ws.send(auth_msg)
        logger.debug("Sent auth message")

    def _dispatch(self, data: Dict[str, Any]) -> None:
        event_type = data.get("type", "")

        try:
            if event_type == "authenticated":
                self._authenticated = True
                logger.info("Authenticated as %s", data.get("address", "unknown"))
                if self.on_authenticated:
                    self.on_authenticated()

            elif event_type == "order_accepted":
                if self.on_order_accepted:
                    self.on_order_accepted(data)

            elif event_type == "order_rejected":
                if self.on_order_rejected:
                    self.on_order_rejected(data)

            elif event_type == "order_fill":
                if self.on_order_fill:
                    self.on_order_fill(data)

            elif event_type in ("order_cancelled", "order_cancelled_v2"):
                if self.on_order_cancelled:
                    self.on_order_cancelled(data)

            elif event_type == "settlement":
                if self.on_settlement:
                    self.on_settlement(data)

            elif event_type == "position_update":
                if self.on_position_update:
                    self.on_position_update(data)

            elif event_type == "redeemable_position":
                if self.on_redeemable_position:
                    self.on_redeemable_position(data)

            elif event_type == "pnl_update":
                if self.on_pnl_update:
                    self.on_pnl_update(data)

            elif event_type == "wallet_status":
                if self.on_wallet_status:
                    self.on_wallet_status(data)

            elif event_type == "balance_update":
                if self.on_balance_update:
                    self.on_balance_update(data)

            elif event_type == "orders_snapshot":
                if self.on_orders_snapshot:
                    self.on_orders_snapshot(data)

            elif event_type == "positions_snapshot":
                if self.on_positions_snapshot:
                    self.on_positions_snapshot(data)

            elif event_type == "error":
                code = data.get("code", "")
                msg = data.get("message", "")
                if code == "INVALID_CREDENTIALS":
                    self._authenticated = False
                    logger.error("Authentication failed: %s", msg)
                    if self.on_auth_error:
                        self.on_auth_error(data)
                else:
                    logger.warning("User WS error [%s]: %s", code, msg)

            elif event_type == "activity_created":
                if self.on_activity_created:
                    self.on_activity_created(data)

            elif event_type == "pong":
                pass

            else:
                logger.debug("Unhandled user event: %s", event_type)
        except Exception:
            logger.exception("User WS callback error for event '%s'", event_type)
