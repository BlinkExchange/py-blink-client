# /home/shanmu/Documents/crypto/blink/py-blink-client/py_blink_client/client.py
"""
Blink Markets CLOB client.

Provides ``ClobClient`` -- the main entry point for interacting with the
Blink prediction-market order book. Drop-in compatible with Polymarket's
``py-clob-client``.

Usage::

    from py_blink_client import ClobClient, OrderArgs, OrderType

    client = ClobClient("https://api.blink15.com", key="0x...")
    creds = client.create_or_derive_api_creds()
    client.set_api_creds(creds)

    markets = client.get_markets()
    token_id = markets["data"][0]["yes_token_id"]
    signed = client.create_order(OrderArgs(token_id=token_id, price=0.55, size=10, side="BUY"))
    resp = client.post_order(signed, orderType=OrderType.GTC)
"""
from __future__ import annotations

import hashlib
import json
import logging
import secrets
from decimal import Decimal, ROUND_DOWN
from typing import Any, Dict, List, Optional, Union

import msgspec

from ._auth import (
    build_hmac_signature,
    create_l1_headers,
    create_l2_headers,
    create_l2_headers_fast,
)
from ._http import BlinkSyncHttpClient
from ._order_builder import (
    ROUNDING_CONFIG,
    build_order_amounts,
    build_market_order_amounts,
    calculate_market_price as _calculate_market_price,
    round_down,
    round_normal,
    to_token_decimals,
)
from ._signing import BlinkSigner, _parse_token_id, canonical_token_id
from .constants import (
    BASE_SEPOLIA_CHAIN_ID,
    COLLATERAL_SCALE,
    CONTRACTS,
    DEFAULT_API_URL,
    DEFAULT_TIMEOUT,
    END_CURSOR,
    EP_ADMIN_HEALTH_DETAILED,
    EP_API_KEY,
    EP_API_KEYS,
    EP_BALANCE_ALLOWANCE,
    EP_BALANCE_ALLOWANCE_UPDATE,
    EP_BALANCE_PUBLIC,
    EP_BOOK,
    EP_BOOK_UNIFIED,
    EP_BOOKS,
    EP_CANCEL_ALL,
    EP_CREATE_API_KEY,
    EP_DATA_ORDER,
    EP_DATA_ORDERS,
    EP_DATA_TRADES,
    EP_DERIVE_API_KEY,
    EP_FAUCET_CLAIM,
    EP_FEE_RATE,
    EP_HEALTH,
    EP_HEALTH_READY,
    EP_HEARTBEATS,
    EP_LAST_TRADE_PRICE,
    EP_MARKET,
    EP_MARKETS,
    EP_MARKETS_RESOLVING,
    EP_MARKETS_UPCOMING,
    EP_MIDPOINT,
    EP_MIDPOINTS,
    EP_NEG_RISK,
    EP_ORDER,
    EP_ORDERS,
    EP_PERMIT,
    EP_PREFUND_CTF_APPROVAL,
    EP_PRICE,
    EP_PRICE_HISTORY,
    EP_PRICES,
    EP_PROFILE,
    EP_SPREAD,
    EP_TICK_SIZE,
    EP_TICKS,
    EP_TICKS_STATS,
    EP_TIME,
    EP_WALLET_STATUS,
    MAX_PAGES,
    ZERO_ADDRESS,
)
from .exceptions import BlinkApiError, BlinkAuthError, BlinkOrderError
from .types import (
    ApiCreds,
    BalanceAllowance,
    BookParams,
    CreateOrderOptions,
    Market,
    MarketOrderArgs,
    OpenOrder,
    OpenOrderParams,
    OrderArgs,
    OrderBookSummary,
    OrderSummary,
    OrderType,
    PartialCreateOrderOptions,
    PostOrdersArgs,
    SignatureType,
    SignedOrder,
    SignedOrderPayload,
    SideInt,
    SubmitOrderRequest,
    SubmitOrderResponse,
    Trade,
    TradeParams,
)

logger = logging.getLogger(__name__)

# Encoder for hot-path msgspec payloads
_msgspec_encoder = msgspec.json.Encoder()


class ClobClient:
    """Client for the Blink Markets CLOB API.

    API-compatible with Polymarket's ``py-clob-client``. Constructor accepts
    both Polymarket-style positional args (``host, chain_id, key``) and Blink
    keyword-style args (``host, private_key``).

    Args:
        host: Base URL of the Blink API (e.g. ``https://api.blink15.com``).
        chain_id: EVM chain ID (default: Base Sepolia 84532).
        key: Hex-encoded Ethereum private key for signing.
        creds: Pre-existing API credentials.
        signature_type: Order signature type (default: EOA).
        funder: Override for the maker/funder address (smart wallets).
        exchange_address: Override for the exchange contract address.
        timeout: HTTP request timeout in seconds.
    """

    def __init__(
        self,
        host: str = DEFAULT_API_URL,
        chain_id: Optional[int] = None,
        key: str = "",
        creds: Optional[ApiCreds] = None,
        signature_type: Union[int, SignatureType] = SignatureType.EOA,
        funder: Optional[str] = None,
        exchange_address: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
        # Backward-compat aliases
        private_key: str = "",
        api_creds: Optional[ApiCreds] = None,
        funder_address: Optional[str] = None,
    ) -> None:
        # -- Input validation --
        if not host or not host.startswith("http"):
            raise ValueError(f"host must be a URL starting with 'http', got '{host}'")

        _pk = key or private_key
        if _pk:
            _pk = _pk.strip()
            if _pk.startswith(("0x", "0X")):
                if len(_pk) != 66:
                    raise ValueError(f"Private key must be 32 bytes (66 hex chars with 0x prefix), got {len(_pk)}")
            else:
                if len(_pk) != 64:
                    raise ValueError(f"Private key must be 32 bytes (64 hex chars), got {len(_pk)}")
                _pk = "0x" + _pk

        _chain = chain_id if chain_id is not None else BASE_SEPOLIA_CHAIN_ID
        if not isinstance(_chain, int) or _chain <= 0:
            raise ValueError(f"chain_id must be a positive integer, got {chain_id!r}")

        self.host = host.rstrip("/")
        self._private_key = _pk
        self.chain_id = _chain
        self.exchange_address = exchange_address or CONTRACTS["exchange"]
        self.creds = creds or api_creds
        self.funder_address = funder or funder_address
        self.signature_type = int(signature_type)
        self.timeout = timeout

        # -- HTTP client --
        self._http = BlinkSyncHttpClient(self.host, timeout=timeout)

        # -- Caches --
        self._tick_size_cache: Dict[str, str] = {}
        self._neg_risk_cache: Dict[str, bool] = {}
        self._fee_rate_cache: Dict[str, int] = {}

        # -- Signer (cached Account + domain separator) --
        self._signer: Optional[BlinkSigner] = None
        self._address: Optional[str] = None
        if self._private_key:
            self._signer = BlinkSigner(self._private_key, self.chain_id, self.exchange_address)
            self._address = self._signer.address

        # -- Pre-decode secret bytes for fast HMAC --
        self._secret_bytes: Optional[bytes] = None
        self._static_l2_headers: Optional[Dict[str, str]] = None
        if self.creds:
            self._init_creds_cache(self.creds)

    def _init_creds_cache(self, creds: ApiCreds) -> None:
        """Pre-compute cached HMAC values from credentials."""
        from base64 import urlsafe_b64decode
        secret = creds.api_secret
        self._secret_bytes = urlsafe_b64decode(secret + "=" * (-len(secret) % 4))
        self._static_l2_headers = {
            "BLINK-ADDRESS": self.address,
            "BLINK-API-KEY": creds.api_key,
            "BLINK-PASSPHRASE": creds.api_passphrase,
        }

    # ------------------------------------------------------------------
    # Dunder methods
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        key_display = f"{self._private_key[:6]}...{self._private_key[-4:]}" if self._private_key else "None"
        return f"ClobClient(host='{self.host}', key='{key_display}')"

    def __enter__(self) -> "ClobClient":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def address(self) -> str:
        """Wallet address derived from the private key."""
        if not self._address:
            raise BlinkAuthError("No private key configured")
        return self._address

    @property
    def maker_address(self) -> str:
        """Address used as the order maker (funder or signer)."""
        return self.funder_address or self.address

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP client."""
        self._http.close()

    # ------------------------------------------------------------------
    # Credential management
    # ------------------------------------------------------------------

    def set_api_creds(self, creds: ApiCreds) -> None:
        """Set HMAC API credentials for authenticated requests.

        Also pre-decodes the secret bytes and builds the static header
        dict for fast L2 header generation on the hot path.
        """
        self.creds = creds
        self._init_creds_cache(creds)

    # ------------------------------------------------------------------
    # Cache warming
    # ------------------------------------------------------------------

    def warm_cache(self, token_ids: List[str]) -> None:
        """Pre-fetch tick sizes, fee rates, and neg risk for token IDs.

        Eliminates API calls from the order hot path. Call this once at
        startup with all token IDs you intend to trade.
        Uses ThreadPoolExecutor for concurrent fetching.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        tids = [canonical_token_id(t) for t in token_ids]

        def _fetch_one(tid: str) -> None:
            self.get_tick_size(tid)
            self.get_fee_rate_bps(tid)
            self.get_neg_risk(tid)

        with ThreadPoolExecutor(max_workers=min(len(tids) * 3, 20)) as pool:
            futures = [pool.submit(_fetch_one, tid) for tid in tids]
            for f in as_completed(futures):
                f.result()  # Raise any exceptions

    # ------------------------------------------------------------------
    # Address helpers (Polymarket compat)
    # ------------------------------------------------------------------

    def get_address(self) -> Optional[str]:
        """Returns the public address of the signer."""
        return self._address

    def get_collateral_address(self) -> str:
        """Returns the USDC collateral token address."""
        return CONTRACTS["usdc"]

    def get_conditional_address(self) -> str:
        """Returns the CTF conditional token address."""
        return CONTRACTS["ctf"]

    def get_exchange_address(self, neg_risk: bool = False) -> str:
        """Returns the exchange contract address."""
        return self.exchange_address

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, str]] = None,
             headers: Optional[Dict[str, str]] = None) -> Any:
        return self._http.get(path, params=params, headers=headers)

    def _post(self, path: str, body: Optional[Any] = None,
              headers: Optional[Dict[str, str]] = None) -> Any:
        return self._http.post(path, body=body, headers=headers)

    def _delete(self, path: str, body: Optional[Any] = None,
                headers: Optional[Dict[str, str]] = None) -> Any:
        return self._http.delete(path, body=body, headers=headers)

    # ------------------------------------------------------------------
    # L1 / L2 header helpers
    # ------------------------------------------------------------------

    def _l1_headers(self, nonce: int = 0) -> Dict[str, str]:
        """Generate L1 (EIP-712) auth headers."""
        if not self._private_key:
            raise BlinkAuthError("Private key required for L1 auth")
        account = self._signer.account if self._signer else None
        return create_l1_headers(
            self._private_key, self.chain_id, nonce=nonce, account=account,
        )

    def _l2_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        """Generate L2 (HMAC) auth headers."""
        if not self.creds:
            raise BlinkAuthError("API credentials required (call create_or_derive_api_creds first)")
        # Use fast path if pre-computed
        if self._static_l2_headers and self._secret_bytes:
            return create_l2_headers_fast(
                self._static_l2_headers, self._secret_bytes, method, path, body,
            )
        return create_l2_headers(
            api_key=self.creds.api_key,
            secret=self.creds.api_secret,
            passphrase=self.creds.api_passphrase,
            address=self.address,
            method=method,
            path=path,
            body=body,
            secret_bytes=self._secret_bytes,
        )

    # ------------------------------------------------------------------
    # Tick size / fee rate / neg risk resolution (with caching)
    # ------------------------------------------------------------------

    def _get_tick_size_for(self, token_id: str) -> str:
        """Get tick size for a token, cached."""
        if token_id not in self._tick_size_cache:
            try:
                resp = self.get_tick_size(token_id)
                self._tick_size_cache[token_id] = str(
                    resp.get("minimum_tick_size", "0.01") if isinstance(resp, dict) else "0.01"
                )
            except Exception:
                logger.warning("Failed to fetch tick size for %s, using default 0.01", token_id)
                self._tick_size_cache[token_id] = "0.01"
        return self._tick_size_cache[token_id]

    def _get_fee_rate_for(self, token_id: str) -> int:
        """Get fee rate for a token, cached."""
        if token_id not in self._fee_rate_cache:
            try:
                resp = self.get_fee_rate_bps(token_id)
                self._fee_rate_cache[token_id] = int(
                    resp.get("base_fee", 0) if isinstance(resp, dict) else 0
                )
            except Exception:
                logger.warning("Failed to fetch fee rate for %s, using default 0", token_id)
                self._fee_rate_cache[token_id] = 0
        return self._fee_rate_cache[token_id]

    def _get_neg_risk_for(self, token_id: str) -> bool:
        """Get neg risk for a token, cached."""
        if token_id not in self._neg_risk_cache:
            try:
                resp = self.get_neg_risk(token_id)
                self._neg_risk_cache[token_id] = bool(
                    resp.get("neg_risk", False) if isinstance(resp, dict) else False
                )
            except Exception:
                logger.warning("Failed to fetch neg_risk for %s, using default False", token_id)
                self._neg_risk_cache[token_id] = False
        return self._neg_risk_cache[token_id]

    # ==================================================================
    # PUBLIC ENDPOINTS (no auth) -- see Task 10
    # ==================================================================

    def get_ok(self) -> Any:
        """Root health check (GET /)."""
        return self._get("/")

    def health(self) -> Any:
        """Check API health (GET /health)."""
        return self._get(EP_HEALTH)

    def health_ready(self) -> Any:
        """Check if the API is ready (GET /health/ready)."""
        return self._get(EP_HEALTH_READY)

    def health_detailed(self, admin_key: str) -> Any:
        """Get detailed health (GET /admin/health/detailed)."""
        return self._get(EP_ADMIN_HEALTH_DETAILED, headers={"X-Admin-Key": admin_key})

    def get_server_time(self) -> Any:
        """Get server time (GET /time)."""
        return self._get(EP_TIME)

    def get_markets(self, next_cursor: Optional[str] = None) -> Dict[str, Any]:
        """Get all markets (Polymarket paginated format)."""
        params: Dict[str, str] = {}
        if next_cursor:
            params["next_cursor"] = next_cursor
        data = self._get(EP_MARKETS, params=params or None)
        if isinstance(data, list):
            return {"data": data, "next_cursor": "", "limit": len(data), "count": len(data)}
        if isinstance(data, dict):
            if "data" in data:
                return data
            markets_list = data.get("markets", [])
            return {"data": markets_list, "next_cursor": "", "limit": len(markets_list), "count": len(markets_list)}
        return {"data": [], "next_cursor": "", "limit": 0, "count": 0}

    def get_simplified_markets(self, next_cursor: Optional[str] = None) -> Dict[str, Any]:
        """Polymarket compat -- delegates to get_markets()."""
        return self.get_markets(next_cursor=next_cursor)

    def get_sampling_simplified_markets(self, next_cursor: Optional[str] = None) -> Dict[str, Any]:
        """Polymarket compat -- delegates to get_markets()."""
        return self.get_markets(next_cursor=next_cursor)

    def get_sampling_markets(self, next_cursor: Optional[str] = None) -> Dict[str, Any]:
        """Polymarket compat -- delegates to get_markets()."""
        return self.get_markets(next_cursor=next_cursor)

    def get_market(self, market_id: str) -> Market:
        """Get a single market by ID."""
        data = self._get(f"{EP_MARKET}{market_id}")
        return Market.from_dict(data)

    def get_order_book(self, token_id: str) -> OrderBookSummary:
        """Get the orderbook for a token."""
        token_id = canonical_token_id(token_id)
        data = self._get(EP_BOOK, params={"token_id": token_id})
        return OrderBookSummary.from_dict(data)

    # Backward-compat alias
    get_book = get_order_book

    def get_order_books(self, params: Union[List[BookParams], List[str]]) -> Any:
        """Get orderbooks for multiple tokens.

        Accepts either a list of BookParams (Polymarket compat) or a list of
        token_id strings. Translates to Blink's ``POST /books`` body format.
        """
        if params and isinstance(params[0], BookParams):
            token_ids = [canonical_token_id(p.token_id) for p in params]
        else:
            token_ids = [canonical_token_id(t) for t in params]
        return self._post(EP_BOOKS, body=json.dumps({"token_ids": token_ids}, separators=(",", ":")))

    def get_order_book_hash(self, orderbook: OrderBookSummary) -> str:
        """Compute the SHA1 hash of an orderbook (Polymarket compat)."""
        payload = {
            "market": orderbook.market,
            "asset_id": orderbook.asset_id,
            "timestamp": orderbook.timestamp,
            "hash": "",
            "bids": [{"price": o.price, "size": o.size} for o in (orderbook.bids or [])],
            "asks": [{"price": o.price, "size": o.size} for o in (orderbook.asks or [])],
            "min_order_size": orderbook.min_order_size,
            "tick_size": orderbook.tick_size,
            "neg_risk": orderbook.neg_risk,
            "last_trade_price": orderbook.last_trade_price,
        }
        serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        h = hashlib.sha1(serialized.encode("utf-8")).hexdigest()
        orderbook.hash = h
        return h

    def get_midpoint(self, token_id: str) -> Any:
        """Get the midpoint price for a token."""
        token_id = canonical_token_id(token_id)
        return self._get(EP_MIDPOINT, params={"token_id": token_id})

    def get_midpoints(self, params: Union[List[BookParams], List[str]]) -> Any:
        """Get midpoints for multiple tokens."""
        if params and isinstance(params[0], BookParams):
            token_ids = [canonical_token_id(p.token_id) for p in params]
        else:
            token_ids = [canonical_token_id(t) for t in params]
        return self._post(EP_MIDPOINTS, body=json.dumps({"token_ids": token_ids}, separators=(",", ":")))

    def get_price(self, token_id: str, side: str = "BUY") -> Any:
        """Get the best price for a token on a given side."""
        token_id = canonical_token_id(token_id)
        return self._get(EP_PRICE, params={"token_id": token_id, "side": str(side)})

    def get_prices(self, params: Union[List[BookParams], List[dict]]) -> Any:
        """Get prices for multiple token/side pairs."""
        if params and isinstance(params[0], BookParams):
            requests = [{"token_id": canonical_token_id(p.token_id), "side": p.side} for p in params]
        else:
            requests = [
                {**r, "token_id": canonical_token_id(r["token_id"])} if isinstance(r, dict) and "token_id" in r else r
                for r in params
            ]
        return self._post(EP_PRICES, body=json.dumps({"requests": requests}, separators=(",", ":")))

    def get_spread(self, token_id: str) -> Any:
        """Get the bid-ask spread for a token."""
        token_id = canonical_token_id(token_id)
        return self._get(EP_SPREAD, params={"token_id": token_id})

    def get_last_trade_price(self, token_id: str) -> Any:
        """Get the last trade price for a token."""
        token_id = canonical_token_id(token_id)
        return self._get(EP_LAST_TRADE_PRICE, params={"token_id": token_id})

    def get_tick_size(self, token_id: str) -> Any:
        """Get the tick size for a token (cached)."""
        token_id = canonical_token_id(token_id)
        if token_id in self._tick_size_cache:
            return {"minimum_tick_size": self._tick_size_cache[token_id]}
        resp = self._get(EP_TICK_SIZE, params={"token_id": token_id})
        if isinstance(resp, dict):
            self._tick_size_cache[token_id] = str(resp.get("minimum_tick_size", "0.01"))
        return resp

    def get_neg_risk(self, token_id: str) -> Any:
        """Get the neg-risk status for a token (cached)."""
        token_id = canonical_token_id(token_id)
        if token_id in self._neg_risk_cache:
            return {"neg_risk": self._neg_risk_cache[token_id]}
        resp = self._get(EP_NEG_RISK, params={"token_id": token_id})
        if isinstance(resp, dict):
            self._neg_risk_cache[token_id] = resp.get("neg_risk", False)
        return resp

    def get_fee_rate_bps(self, token_id: str) -> Any:
        """Get the fee rate for a token (cached)."""
        token_id = canonical_token_id(token_id)
        if token_id in self._fee_rate_cache:
            return {"base_fee": self._fee_rate_cache[token_id]}
        resp = self._get(EP_FEE_RATE, params={"token_id": token_id})
        if isinstance(resp, dict):
            self._fee_rate_cache[token_id] = resp.get("base_fee", 0)
        return resp

    # ==================================================================
    # BLINK-SPECIFIC MARKET DATA
    # ==================================================================

    def get_tick_backfill(self, symbol: str) -> Any:
        """Get tick history for a symbol (GET /ticks/{symbol})."""
        return self._get(f"{EP_TICKS}{symbol}")

    def get_price_stats(self, symbol: str) -> Any:
        """Get price statistics for a symbol (GET /ticks/{symbol}/stats)."""
        return self._get(f"{EP_TICKS_STATS}{symbol}/stats")

    def get_price_history(self, token_id: str) -> Any:
        """Get price history for a token (GET /prices/{token_id}/history)."""
        token_id = canonical_token_id(token_id)
        return self._get(f"{EP_PRICE_HISTORY}{token_id}/history")

    def get_upcoming_markets(self, limit: int = 10) -> Any:
        """Get upcoming markets."""
        return self._get(EP_MARKETS_UPCOMING, params={"limit": str(limit)})

    def get_resolving_markets(self) -> Any:
        """Get markets in resolving state."""
        return self._get(EP_MARKETS_RESOLVING)

    def get_market_orderbook(self, market_id: str, token_id: Optional[str] = None) -> Any:
        """Get orderbook for a specific market."""
        params: Optional[Dict[str, str]] = None
        if token_id:
            params = {"token_id": canonical_token_id(token_id)}
        return self._get(f"{EP_MARKET}{market_id}/orderbook", params=params)

    def get_unified_order_book(self, token_id: str) -> Any:
        """Get unified orderbook (GET /book/unified?token_id=)."""
        token_id = canonical_token_id(token_id)
        return self._get(EP_BOOK_UNIFIED, params={"token_id": token_id})

    # ==================================================================
    # FAUCET / GAS SPONSORSHIP (public, no auth)
    # ==================================================================

    def claim_faucet(self, address: Optional[str] = None) -> Any:
        """Claim testnet USDC from the faucet."""
        addr = address or self._address
        if not addr:
            raise BlinkAuthError("Address required for faucet claim")
        return self._post(EP_FAUCET_CLAIM, body=json.dumps({"address": addr}, separators=(",", ":")))

    def relay_permit(self, owner: str, spender: str, value: str,
                     deadline: str, v: int, r: str, s: str) -> Any:
        """Relay a gas-free USDC EIP-2612 permit."""
        body = {"owner": owner, "spender": spender, "value": value,
                "deadline": deadline, "v": v, "r": r, "s": s}
        return self._post(EP_PERMIT, body=json.dumps(body, separators=(",", ":")))

    def prefund_ctf_approval(self, address: Optional[str] = None) -> Any:
        """Sponsor ETH for a CTF approval transaction."""
        addr = address or self._address
        if not addr:
            raise BlinkAuthError("Address required")
        return self._post(EP_PREFUND_CTF_APPROVAL, body=json.dumps({"address": addr}, separators=(",", ":")))

    # ==================================================================
    # BALANCE (public + authenticated)
    # ==================================================================

    def get_balance_allowance(self, asset_type: str = "COLLATERAL",
                              token_id: Optional[str] = None) -> BalanceAllowance:
        """Get balance and allowance (L2 auth)."""
        params: Dict[str, str] = {"asset_type": asset_type}
        if token_id:
            params["token_id"] = canonical_token_id(token_id)
        headers = self._l2_headers("GET", EP_BALANCE_ALLOWANCE)
        data = self._get(EP_BALANCE_ALLOWANCE, params=params, headers=headers)
        return BalanceAllowance.from_dict(data)

    def update_balance_allowance(self, asset_type: str = "COLLATERAL",
                                 token_id: Optional[str] = None) -> Any:
        """Force-refresh cached balance/allowance (L2 auth)."""
        params: Dict[str, str] = {"asset_type": asset_type}
        if token_id:
            params["token_id"] = canonical_token_id(token_id)
        headers = self._l2_headers("GET", EP_BALANCE_ALLOWANCE_UPDATE)
        return self._get(EP_BALANCE_ALLOWANCE_UPDATE, params=params, headers=headers)

    def get_balance_allowance_public(self, address: str,
                                     asset_type: str = "COLLATERAL",
                                     token_id: Optional[str] = None) -> Any:
        """Get balance/allowance without auth (public)."""
        params: Dict[str, str] = {"address": address, "asset_type": asset_type}
        if token_id:
            params["token_id"] = canonical_token_id(token_id)
        return self._get(EP_BALANCE_PUBLIC, params=params)

    def get_wallet_status(self, address: Optional[str] = None) -> Any:
        """Get wallet status."""
        addr = address or self._address
        if not addr:
            raise BlinkAuthError("Address required")
        return self._get(EP_WALLET_STATUS, params={"address": addr})

    # ==================================================================
    # USER DATA (public, no auth)
    # ==================================================================

    def get_user_profile(self, address: str) -> Any:
        """Get a user's profile."""
        return self._get(f"/users/{address}")

    def get_user_positions(self, address: str) -> Any:
        """Get positions for a user address."""
        return self._get(f"/users/{address}/positions")

    def get_user_trades(self, address: str) -> Any:
        """Get trades for a user address."""
        return self._get(f"/users/{address}/trades")

    def get_user_orders(self, address: str) -> Any:
        """Get orders for a user address."""
        return self._get(f"/users/{address}/orders")

    def get_user_activity(self, address: str, limit: int = 50,
                          offset: int = 0, cursor: Optional[str] = None) -> Any:
        """Get activity feed for a user address."""
        params: Dict[str, str] = {"limit": str(limit)}
        if cursor is not None:
            params["cursor"] = cursor
        else:
            params["offset"] = str(offset)
        return self._get(f"/users/{address}/activity", params=params)

    def get_user_redemptions(self, address: str) -> Any:
        """Get redemptions for a user address."""
        return self._get(f"/users/{address}/redemptions")

    def get_user_history(self, address: str) -> Any:
        """Get on-chain event history for a user address."""
        return self._get(f"/users/{address}/history")

    def get_portfolio_history(self, address: str, range: str = "1D") -> Any:
        """Get portfolio history milestones."""
        return self._get(f"/users/{address}/portfolio-history", params={"range": range})

    def search_users(self, query: Optional[str] = None) -> Any:
        """Search users."""
        params: Dict[str, str] = {}
        if query is not None:
            params["q"] = query
        return self._get("/users/search", params=params or None)

    # ==================================================================
    # PROFILE (Privy JWT auth)
    # ==================================================================

    def update_profile(self, token: str, data: Dict[str, str]) -> Any:
        """Update user profile (Privy JWT auth)."""
        body_str = json.dumps(data, separators=(",", ":"))
        return self._post(EP_PROFILE, body=body_str,
                         headers={"Authorization": f"Bearer {token}"})

    # ==================================================================
    # API KEY MANAGEMENT (L1 auth)
    # ==================================================================

    def create_api_key(self, nonce: int = 0) -> ApiCreds:
        """Create a new API key via L1 (EIP-712) auth."""
        headers = self._l1_headers(nonce=nonce)
        data = self._post(EP_CREATE_API_KEY, headers=headers)
        creds = ApiCreds.from_dict(data)
        self.set_api_creds(creds)
        logger.info("Created API key: %s...", creds.api_key[:8])
        return creds

    def derive_api_key(self, nonce: int = 0) -> ApiCreds:
        """Derive an existing API key via L1 (EIP-712) auth."""
        headers = self._l1_headers(nonce=nonce)
        data = self._get(EP_DERIVE_API_KEY, headers=headers)
        creds = ApiCreds.from_dict(data)
        self.set_api_creds(creds)
        logger.info("Derived API key: %s...", creds.api_key[:8])
        return creds

    def create_or_derive_api_creds(self, nonce: int = 0) -> ApiCreds:
        """Create or derive API credentials."""
        try:
            return self.create_api_key(nonce=nonce)
        except BlinkApiError as exc:
            if exc.status_code in (400, 409):
                logger.info("API key already exists, deriving...")
                return self.derive_api_key(nonce=nonce)
            raise

    # Backward-compat alias
    create_or_derive_api_key = create_or_derive_api_creds

    def get_api_keys(self) -> Any:
        """List all API keys (L2 auth)."""
        headers = self._l2_headers("GET", EP_API_KEYS)
        return self._get(EP_API_KEYS, headers=headers)

    # Backward-compat alias
    list_api_keys = get_api_keys

    def delete_api_key(self) -> Any:
        """Delete the current API key (L2 auth)."""
        headers = self._l2_headers("DELETE", EP_API_KEY)
        return self._delete(EP_API_KEY, headers=headers)

    # ==================================================================
    # ORDER SIGNING (hot path)
    # ==================================================================

    def _build_signed_order(self, args: OrderArgs) -> SignedOrder:
        """Build and EIP-712 sign an order from user-friendly args."""
        if not self._signer:
            raise BlinkAuthError("Private key required for order signing")

        tick_size = self._get_tick_size_for(args.token_id)
        fee_rate_bps = self._get_fee_rate_for(args.token_id)
        if args.fee_rate_bps == 0:
            args.fee_rate_bps = fee_rate_bps

        rc = ROUNDING_CONFIG.get(tick_size, ROUNDING_CONFIG["0.01"])
        side_int, maker_amount, taker_amount = build_order_amounts(
            args.side, args.price, args.size, rc,
        )

        if maker_amount <= 0 or taker_amount <= 0:
            raise BlinkOrderError(
                f"Invalid order amounts: maker={maker_amount}, taker={taker_amount} "
                f"(price={args.price}, size={args.size})"
            )

        taker = args.taker if args.taker else ZERO_ADDRESS
        salt = str(secrets.randbits(64))
        maker = self.maker_address
        signer = self.address

        order_data = {
            "salt": salt,
            "maker": maker,
            "signer": signer,
            "taker": taker,
            "tokenId": args.token_id,
            "makerAmount": str(maker_amount),
            "takerAmount": str(taker_amount),
            "expiration": str(args.expiration),
            "nonce": str(args.nonce),
            "feeRateBps": str(args.fee_rate_bps),
            "side": int(side_int),
            "signatureType": self.signature_type,
        }

        signature = self._signer.sign_order(order_data)

        return SignedOrder(
            salt=salt, maker=maker, signer=signer, taker=taker,
            token_id=args.token_id, maker_amount=str(maker_amount),
            taker_amount=str(taker_amount), expiration=str(args.expiration),
            nonce=str(args.nonce), fee_rate_bps=str(args.fee_rate_bps),
            side=side_int, signature_type=SignatureType(self.signature_type),
            signature=signature,
        )

    def create_order(self, args: OrderArgs, options: Any = None) -> Dict[str, Any]:
        """Build and sign an order locally (does NOT submit).

        Returns a plain dict with camelCase keys (Polymarket wire format).
        """
        signed = self._build_signed_order(args)
        return signed.to_dict()

    def post_order(
        self,
        order: Union[Dict[str, Any], SignedOrder],
        orderType: str = OrderType.GTC,
        post_only: bool = False,
        deferExec: bool = False,
        order_type: Optional[str] = None,
    ) -> SubmitOrderResponse:
        """Submit a pre-signed order to the exchange (L2 HMAC auth).

        Uses msgspec for fast payload serialization on the hot path.

        Returns a :class:`~py_blink_client.types.SubmitOrderResponse`
        with ``order_id``, ``order_hash``, ``status``, and ``matches``.
        The full server payload is preserved on ``.raw`` for
        forward-compat.

        ``post_only=True`` only applies to resting order types (``GTC``
        and ``GTD``).  If a non-resting type (``FOK``/``IOC``) is used
        with ``post_only=True`` the flag is downgraded to ``False`` and
        a warning is logged — passing ``post_only`` with an immediate
        order is almost certainly a caller bug, but raising would break
        existing callers.
        """
        if not self.creds:
            raise BlinkAuthError("API credentials required")

        ot = order_type or orderType

        # Fix 7: Silent flag downgrades hide intent — warn, don't fail.
        effective_post_only = post_only and ot in (OrderType.GTC, OrderType.GTD)
        if post_only and not effective_post_only:
            logger.warning(
                "post_only=True ignored for order_type=%s "
                "(post_only only applies to GTC/GTD).", ot,
            )

        # Build the order dict
        if isinstance(order, SignedOrder):
            order_dict = order.to_dict()
        elif isinstance(order, dict):
            order_dict = order
        else:
            raise BlinkOrderError(f"Expected dict or SignedOrder, got {type(order)}")

        # Build payload using msgspec for fast encode
        payload_struct = SubmitOrderRequest(
            order=SignedOrderPayload(
                salt=str(order_dict.get("salt", "")),
                maker=order_dict.get("maker", ""),
                signer=order_dict.get("signer", ""),
                taker=order_dict.get("taker", ""),
                tokenId=str(order_dict.get("tokenId", order_dict.get("token_id", ""))),
                makerAmount=str(order_dict.get("makerAmount", order_dict.get("maker_amount", ""))),
                takerAmount=str(order_dict.get("takerAmount", order_dict.get("taker_amount", ""))),
                expiration=str(order_dict.get("expiration", "0")),
                nonce=str(order_dict.get("nonce", "0")),
                feeRateBps=str(order_dict.get("feeRateBps", order_dict.get("fee_rate_bps", "0"))),
                side=str(order_dict.get("side", "BUY")),
                signatureType=int(order_dict.get("signatureType", order_dict.get("signature_type", 0))),
                signature=order_dict.get("signature", ""),
            ),
            owner=self.creds.api_key,
            order_type=ot,
            post_only=effective_post_only,
        )

        body_bytes = _msgspec_encoder.encode(payload_struct)
        body_str = body_bytes.decode()
        headers = self._l2_headers("POST", EP_ORDER, body_str)
        raw = self._post(EP_ORDER, body=body_bytes, headers=headers)
        # Fix 5: Typed response — forward-compatible via `raw` escape hatch.
        return SubmitOrderResponse.from_dict(raw if isinstance(raw, dict) else {})

    def create_and_post_order(
        self,
        args: OrderArgs,
        orderType: str = OrderType.GTC,
        post_only: bool = False,
        order_type: Optional[str] = None,
        options: Any = None,
    ) -> Any:
        """Build, sign, and submit an order in one call."""
        ot = order_type or orderType
        signed_dict = self.create_order(args, options=options)
        return self.post_order(signed_dict, orderType=ot, post_only=post_only)

    def create_and_post_market_order(
        self,
        args: MarketOrderArgs,
        orderType: str = OrderType.FOK,
        order_type: Optional[str] = None,
        options: Any = None,
    ) -> Any:
        """Build, sign, and submit a market order."""
        ot = order_type or orderType
        signed = self.create_market_order(args, options=options)
        return self.post_order(signed, orderType=ot)

    def post_orders(self, orders: List[Union[Dict[str, Any], PostOrdersArgs]]) -> List[SubmitOrderResponse]:
        """Submit multiple pre-signed orders (max 10, L2 auth).

        Returns a parallel list of
        :class:`~py_blink_client.types.SubmitOrderResponse`, one per
        submitted order.  If the backend returns anything other than a
        list (e.g. an error envelope) the result is a single-element
        list whose ``.raw`` contains the full payload.
        """
        if not self.creds:
            raise BlinkAuthError("API credentials required")
        if len(orders) > 10:
            raise BlinkOrderError("Batch order limit is 10")

        payloads = []
        for o in orders:
            if isinstance(o, PostOrdersArgs):
                payloads.append({
                    "order": o.order,
                    "owner": self.creds.api_key,
                    "order_type": o.orderType,
                    "post_only": o.postOnly,
                })
            else:
                payloads.append(o)

        body_str = json.dumps(payloads, separators=(",", ":"), sort_keys=False)
        headers = self._l2_headers("POST", EP_ORDERS, body_str)
        raw = self._post(EP_ORDERS, body=body_str, headers=headers)
        # Fix 5: Typed response — forward-compatible via `raw` escape hatch.
        if isinstance(raw, list):
            return [SubmitOrderResponse.from_dict(r if isinstance(r, dict) else {}) for r in raw]
        return [SubmitOrderResponse.from_dict(raw if isinstance(raw, dict) else {})]

    def cancel(self, order_id: str) -> Any:
        """Cancel a single order (L2 auth)."""
        body = {"order_id": order_id}
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._l2_headers("DELETE", EP_ORDER, body_str)
        return self._delete(EP_ORDER, body=body_str, headers=headers)

    # Backward-compat alias
    cancel_order = cancel

    def cancel_orders(self, order_ids: List[str]) -> Any:
        """Cancel multiple orders (L2 auth)."""
        body_str = json.dumps(order_ids, separators=(",", ":"))
        headers = self._l2_headers("DELETE", EP_ORDERS, body_str)
        return self._delete(EP_ORDERS, body=body_str, headers=headers)

    def cancel_all(self) -> Any:
        """Cancel all open orders (L2 auth)."""
        headers = self._l2_headers("DELETE", EP_CANCEL_ALL)
        return self._delete(EP_CANCEL_ALL, headers=headers)

    def cancel_market_orders(self, market: Optional[str] = None,
                             asset_id: Optional[str] = None) -> Any:
        """Cancel orders for a specific market/asset (client-side filter)."""
        asset_id_norm = canonical_token_id(asset_id) if asset_id else None
        all_orders = self.get_orders()
        to_cancel: List[str] = []
        for o in all_orders:
            oid = o.get("order_id", o.get("id", ""))
            if not oid:
                continue
            if market and o.get("market_id", o.get("market", "")) != market:
                continue
            if asset_id_norm and o.get("asset_id", o.get("token_id", "")) != asset_id_norm:
                continue
            to_cancel.append(oid)

        if not to_cancel:
            return {"canceled": [], "not_canceled": []}
        return self.cancel_orders(to_cancel)

    # ==================================================================
    # DATA QUERIES (L2 auth)
    # ==================================================================

    def get_orders(self, params: Union[None, Dict[str, str], OpenOrderParams] = None) -> List[Dict[str, Any]]:
        """Get open orders, forwarding filter params to the server.

        Mirrors the async client: the server accepts ``market_id`` /
        ``asset_id`` / ``id`` via L2 HMAC auth, so we forward
        ``OpenOrderParams.to_dict()`` as the query string instead of
        fetching everything and filtering locally.  A post-fetch refine
        stays so any field the server doesn't filter on still narrows.
        """
        query: Optional[Dict[str, str]] = None
        if params is not None:
            if hasattr(params, "to_dict"):
                query = params.to_dict()
            elif isinstance(params, dict):
                query = dict(params)

        headers = self._l2_headers("GET", EP_DATA_ORDERS)
        data = self._get(EP_DATA_ORDERS, params=query or None, headers=headers)

        results: List[Dict[str, Any]] = []
        if isinstance(data, list):
            results = data
        elif isinstance(data, dict):
            results = data.get("data", data.get("orders", []))

        # Client-side refinement -- server has already filtered on the
        # fields it knows about; this catches any remaining mismatches
        # (alternate field names in legacy responses).
        if isinstance(params, OpenOrderParams):
            if params.market:
                results = [o for o in results if o.get("market", o.get("market_id", "")) == params.market]
            if params.asset_id:
                results = [o for o in results if o.get("asset_id", o.get("token_id", "")) == params.asset_id]
            if params.id:
                results = [o for o in results if o.get("id", o.get("order_id", "")) == params.id]
        return results

    def get_order(self, order_id: str) -> OpenOrder:
        """Get a single order by ID (L2 auth)."""
        path = f"{EP_DATA_ORDER}{order_id}"
        headers = self._l2_headers("GET", path)
        data = self._get(path, headers=headers)
        return OpenOrder.from_dict(data)

    def get_trades(self, params: Union[None, str, Dict[str, str], TradeParams] = None) -> List[Dict[str, Any]]:
        """Get all trades (single fetch, client-side filter)."""
        query: Dict[str, str] = {}
        if params is not None:
            if hasattr(params, "to_dict"):
                query = params.to_dict()
            elif isinstance(params, dict):
                query = dict(params)
            elif isinstance(params, str):
                query = {"market_id": params}

        headers = self._l2_headers("GET", EP_DATA_TRADES)
        data = self._get(EP_DATA_TRADES, params=query or None, headers=headers)

        results: List[Dict[str, Any]] = []
        if isinstance(data, list):
            results = data
        elif isinstance(data, dict):
            results = data.get("data", data.get("trades", []))
        return results

    def get_trades_paginated(self, params: Union[None, TradeParams] = None,
                             next_cursor: Optional[str] = None) -> Dict[str, Any]:
        """Get trades with raw cursor response (Polymarket compat)."""
        query: Dict[str, str] = {}
        if params is not None and hasattr(params, "to_dict"):
            query = params.to_dict()
        if next_cursor:
            query["next_cursor"] = next_cursor

        headers = self._l2_headers("GET", EP_DATA_TRADES)
        return self._get(EP_DATA_TRADES, params=query or None, headers=headers)

    def post_heartbeat(self, heartbeat_id: Optional[str] = None) -> Any:
        """Send a heartbeat (L2 auth)."""
        hb_id = heartbeat_id or secrets.token_hex(16)
        body = {"heartbeat_id": hb_id}
        body_str = json.dumps(body, separators=(",", ":"))
        headers = self._l2_headers("POST", EP_HEARTBEATS, body_str)
        return self._post(EP_HEARTBEATS, body=body_str, headers=headers)

    def calculate_market_price(self, token_id: str, side: str, amount: float,
                               order_type: str = OrderType.FOK) -> Optional[float]:
        """Walk the orderbook to find the worst price needed to fill an amount."""
        book = self.get_order_book(token_id)
        return _calculate_market_price(book, side, amount, order_type)

    def create_market_order(self, args: MarketOrderArgs, options: Any = None) -> Dict[str, Any]:
        """Build and sign a market order by walking the orderbook."""
        side = str(args.side)
        price = args.price if args.price and args.price > 0 else self.calculate_market_price(
            args.token_id, side, args.amount, args.order_type,
        )
        if price is None or price <= 0:
            raise BlinkOrderError(
                f"Insufficient liquidity for market order: "
                f"token_id={args.token_id}, side={side}, amount={args.amount}"
            )

        # Convert dollar amount to shares for BUY
        if side == "BUY":
            size = args.amount / price
        else:
            size = args.amount

        order_args = OrderArgs(
            token_id=args.token_id, price=price, size=size, side=side,
            fee_rate_bps=args.fee_rate_bps, nonce=args.nonce, taker=args.taker,
        )
        return self.create_order(order_args)

    # ==================================================================
    # BLINK-SPECIFIC ORDER QUERIES (L2 auth)
    # ==================================================================

    def list_orders(self, market_id: Optional[str] = None, maker: Optional[str] = None,
                    status: Optional[str] = None, limit: Optional[int] = None) -> Any:
        """List orders via /orders endpoint (richer filtering).

        The server honours the ``maker`` filter; when no maker is supplied
        the backend returns ``[]`` instead of treating the caller as the
        maker.  To avoid a silently-empty result we default ``maker`` to
        the signer's address when the client has one.  Pass
        ``maker=""`` explicitly to opt out of this default.
        """
        # Fix 4: Default `maker` so callers aren't surprised by an empty list.
        if maker is None and self._address:
            maker = self._address

        params: Dict[str, str] = {}
        if market_id is not None:
            params["market_id"] = market_id
        if maker:
            params["maker"] = maker
        if status is not None:
            params["status"] = status
        if limit is not None:
            params["limit"] = str(limit)
        headers = self._l2_headers("GET", EP_ORDERS)
        return self._get(EP_ORDERS, params=params, headers=headers)

    def get_order_by_id(self, order_id: str) -> Any:
        """Get a single order via /orders/{id} endpoint."""
        path = f"{EP_ORDERS}/{order_id}"
        headers = self._l2_headers("GET", path)
        return self._get(path, headers=headers)

    # ==================================================================
    # POLYMARKET STUBS (NotImplementedError)
    # ==================================================================

    def get_notifications(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def drop_notifications(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def is_order_scoring(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def are_orders_scoring(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_earnings_for_user_for_day(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_total_earnings_for_user_for_day(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_reward_percentages(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_current_rewards(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_raw_rewards_for_market(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_user_earnings_and_markets_config(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_closed_only_mode(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def create_readonly_api_key(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_readonly_api_keys(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def delete_readonly_api_key(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def validate_readonly_api_key(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_market_trades_events(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def create_builder_api_key(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_builder_api_keys(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def revoke_builder_api_key(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_builder_trades(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_last_trades_prices(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_spreads(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    def get_prices_history(self, *args: Any, **kwargs: Any) -> Any:
        """Polymarket compat -- delegates to get_price_history()."""
        if args:
            return self.get_price_history(str(args[0]))
        raise NotImplementedError("This Polymarket feature is not available on Blink")

    @property
    def rfq(self) -> Any:
        """Polymarket RFQ stub -- all methods throw NotImplementedError."""
        return _RfqStub()


class _RfqStub:
    """Stub RFQ client where all methods throw NotImplementedError."""
    def __getattr__(self, name: str) -> Any:
        def _not_impl(*args: Any, **kwargs: Any) -> Any:
            raise NotImplementedError("This Polymarket feature is not available on Blink")
        return _not_impl


# Backward-compat alias
BlinkClobClient = ClobClient
