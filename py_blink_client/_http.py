# /home/shanmu/Documents/crypto/blink/py-blink-client/py_blink_client/_http.py
"""
HTTP transport layer.

- ``BlinkHttpClient`` -- async, aiohttp-backed, separate trading + data pools.
- ``BlinkSyncHttpClient`` -- sync, httpx-backed, single pool (fine for sync).

Both implement retry (3 attempts, exponential backoff, 429 Retry-After),
30s default timeout, and orjson for unstructured response parsing.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Union

import orjson

from .constants import BACKOFF_FACTORS, DEFAULT_RETRIES, DEFAULT_TIMEOUT
from .exceptions import BlinkApiError

logger = logging.getLogger(__name__)

# Endpoints that go through the trading pool (latency-sensitive)
_TRADING_PATHS = frozenset({
    "/order", "/orders", "/cancel-all", "/v1/heartbeats",
})


def _is_trading_path(path: str) -> bool:
    """Check if a path should use the trading connection pool."""
    return path in _TRADING_PATHS


# ---------------------------------------------------------------------------
# Async HTTP client (aiohttp)
# ---------------------------------------------------------------------------

class BlinkHttpClient:
    """Async HTTP client backed by aiohttp with dual connection pools.

    - Trading pool (limit=10): POST /order, DELETE /order, etc.
    - Data pool (limit=100): all read endpoints.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
        trading_pool_size: int = 10,
        data_pool_size: int = 100,
    ) -> None:
        import aiohttp

        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)

        # Connectors for dual pool
        self._trading_connector = aiohttp.TCPConnector(
            limit_per_host=trading_pool_size, enable_cleanup_closed=True,
        )
        self._data_connector = aiohttp.TCPConnector(
            limit_per_host=data_pool_size, enable_cleanup_closed=True,
        )

        self._trading_session: Optional[aiohttp.ClientSession] = None
        self._data_session: Optional[aiohttp.ClientSession] = None

    async def _ensure_sessions(self) -> None:
        import aiohttp
        if self._trading_session is None or self._trading_session.closed:
            self._trading_session = aiohttp.ClientSession(
                connector=self._trading_connector,
                timeout=self._timeout,
                connector_owner=False,
            )
        if self._data_session is None or self._data_session.closed:
            self._data_session = aiohttp.ClientSession(
                connector=self._data_connector,
                timeout=self._timeout,
                connector_owner=False,
            )

    def _session_for(self, path: str) -> Any:
        """Select the appropriate session based on the path."""
        if _is_trading_path(path):
            return self._trading_session
        return self._data_session

    async def request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Union[str, bytes]] = None,
    ) -> Any:
        """Execute an HTTP request with retry logic.

        Returns parsed JSON (via orjson) or raw text.
        """
        await self._ensure_sessions()
        session = self._session_for(path)
        url = f"{self._base_url}{path}"
        last_exc: Optional[Exception] = None

        for attempt in range(DEFAULT_RETRIES):
            try:
                kwargs: Dict[str, Any] = {}
                if headers:
                    kwargs["headers"] = headers
                if params:
                    kwargs["params"] = params
                if body is not None:
                    if isinstance(body, (str, bytes)):
                        kwargs["data"] = body
                        if "headers" not in kwargs:
                            kwargs["headers"] = {}
                        kwargs["headers"]["Content-Type"] = "application/json"
                    else:
                        kwargs["json"] = body

                async with session.request(method, url, **kwargs) as resp:
                    raw = await resp.read()

                    if 200 <= resp.status < 300:
                        if raw:
                            try:
                                return orjson.loads(raw)
                            except orjson.JSONDecodeError:
                                return raw.decode("utf-8", errors="replace")
                        return ""

                    body_text = raw.decode("utf-8", errors="replace")

                    # 429 -- rate limited
                    if resp.status == 429:
                        retry_after = float(resp.headers.get(
                            "Retry-After",
                            BACKOFF_FACTORS[min(attempt, len(BACKOFF_FACTORS) - 1)],
                        ))
                        logger.warning("Rate limited (429), retrying in %.1fs", retry_after)
                        await asyncio.sleep(retry_after)
                        last_exc = BlinkApiError(resp.status, body_text, method, path)
                        continue

                    # 4xx -- no retry
                    if 400 <= resp.status < 500:
                        raise BlinkApiError(resp.status, body_text, method, path)

                    # 5xx -- retry
                    last_exc = BlinkApiError(resp.status, body_text, method, path)

            except BlinkApiError:
                raise
            except Exception as exc:
                last_exc = exc  # type: ignore[assignment]

            # Backoff before retry
            if attempt < DEFAULT_RETRIES - 1:
                delay = BACKOFF_FACTORS[attempt] if attempt < len(BACKOFF_FACTORS) else BACKOFF_FACTORS[-1]
                logger.warning(
                    "Request %s %s failed (attempt %d/%d), retrying in %.1fs",
                    method, url, attempt + 1, DEFAULT_RETRIES, delay,
                )
                await asyncio.sleep(delay)

        if isinstance(last_exc, BlinkApiError):
            raise last_exc
        raise BlinkApiError(0, str(last_exc), method, path)

    async def get(self, path: str, params: Optional[Dict[str, str]] = None,
                  headers: Optional[Dict[str, str]] = None) -> Any:
        return await self.request("GET", path, headers=headers, params=params)

    async def post(self, path: str, body: Optional[Union[str, bytes]] = None,
                   headers: Optional[Dict[str, str]] = None) -> Any:
        return await self.request("POST", path, headers=headers, body=body)

    async def delete(self, path: str, body: Optional[Union[str, bytes]] = None,
                     headers: Optional[Dict[str, str]] = None) -> Any:
        return await self.request("DELETE", path, headers=headers, body=body)

    async def close(self) -> None:
        """Close both sessions and connectors."""
        if self._trading_session and not self._trading_session.closed:
            await self._trading_session.close()
        if self._data_session and not self._data_session.closed:
            await self._data_session.close()
        if not self._trading_connector.closed:
            await self._trading_connector.close()
        if not self._data_connector.closed:
            await self._data_connector.close()


# ---------------------------------------------------------------------------
# Sync HTTP client (httpx)
# ---------------------------------------------------------------------------

class BlinkSyncHttpClient:
    """Sync HTTP client backed by httpx.

    Single connection pool (fine for sync workloads).
    Implements the same retry logic as BlinkHttpClient.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        import httpx

        self._base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout,
            follow_redirects=True,
        )

    def request(
        self,
        method: str,
        path: str,
        headers: Optional[Dict[str, str]] = None,
        params: Optional[Dict[str, str]] = None,
        body: Optional[Union[str, bytes, dict, list]] = None,
    ) -> Any:
        """Execute an HTTP request with retry logic."""
        last_exc: Optional[Exception] = None

        for attempt in range(DEFAULT_RETRIES):
            try:
                kwargs: Dict[str, Any] = {}
                if headers:
                    kwargs["headers"] = headers
                if params:
                    kwargs["params"] = params
                if body is not None:
                    if isinstance(body, (str, bytes)):
                        kwargs["content"] = body if isinstance(body, bytes) else body.encode()
                        if "headers" not in kwargs:
                            kwargs["headers"] = {}
                        kwargs["headers"]["Content-Type"] = "application/json"
                    else:
                        kwargs["json"] = body

                resp = self._client.request(method, path, **kwargs)

                if resp.is_success:
                    if resp.content:
                        try:
                            return orjson.loads(resp.content)
                        except orjson.JSONDecodeError:
                            return resp.text
                    return ""

                body_text = resp.text

                # 429 -- rate limited
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get(
                        "Retry-After",
                        BACKOFF_FACTORS[min(attempt, len(BACKOFF_FACTORS) - 1)],
                    ))
                    logger.warning("Rate limited (429), retrying in %.1fs", retry_after)
                    time.sleep(retry_after)
                    last_exc = BlinkApiError(resp.status_code, body_text, method, path)
                    continue

                # 4xx -- no retry
                if 400 <= resp.status_code < 500:
                    raise BlinkApiError(resp.status_code, body_text, method, path)

                # 5xx -- retry
                last_exc = BlinkApiError(resp.status_code, body_text, method, path)

            except BlinkApiError:
                raise
            except Exception as exc:
                last_exc = exc  # type: ignore[assignment]

            if attempt < DEFAULT_RETRIES - 1:
                delay = BACKOFF_FACTORS[attempt] if attempt < len(BACKOFF_FACTORS) else BACKOFF_FACTORS[-1]
                logger.warning(
                    "Request %s %s failed (attempt %d/%d), retrying in %.1fs",
                    method, path, attempt + 1, DEFAULT_RETRIES, delay,
                )
                time.sleep(delay)

        if isinstance(last_exc, BlinkApiError):
            raise last_exc
        raise BlinkApiError(0, str(last_exc), method, path)

    def get(self, path: str, params: Optional[Dict[str, str]] = None,
            headers: Optional[Dict[str, str]] = None) -> Any:
        return self.request("GET", path, headers=headers, params=params)

    def post(self, path: str, body: Optional[Union[str, bytes]] = None,
             headers: Optional[Dict[str, str]] = None) -> Any:
        return self.request("POST", path, headers=headers, body=body)

    def delete(self, path: str, body: Optional[Union[str, bytes]] = None,
               headers: Optional[Dict[str, str]] = None) -> Any:
        return self.request("DELETE", path, headers=headers, body=body)

    def close(self) -> None:
        """Close the underlying httpx client."""
        self._client.close()
