"""
py-blink-client -- Python client for the Blink Markets CLOB API.

Drop-in compatible with Polymarket's ``py-clob-client``.

Quick start::

    from py_blink_client import ClobClient, OrderArgs, OrderType

    client = ClobClient("https://api.blink15.com", key="0x...")
    creds = client.create_or_derive_api_creds()

    markets = client.get_markets()
    signed = client.create_order(OrderArgs(
        token_id=markets["data"][0]["yes_token_id"],
        side="BUY",
        price=0.55,
        size=10,
    ))
    resp = client.post_order(signed, OrderType.GTC)
"""

from .client import ClobClient, BlinkClobClient
try:
    from .async_client import AsyncClobClient
except ImportError:
    AsyncClobClient = None  # type: ignore[assignment,misc]
from .types import (
    ApiCreds,
    BalanceAllowance,
    BalanceAllowanceParams,
    BookParams,
    CreateOrderOptions,
    Market,
    MarketOrderArgs,
    MatchInfo,
    OpenOrder,
    OpenOrderParams,
    OrderArgs,
    OrderBook,
    OrderBookSummary,
    OrderSummary,
    OrderPayload,
    OrderType,
    PartialCreateOrderOptions,
    PostOrdersArgs,
    RoundConfig,
    Side,
    SignatureType,
    SignedOrder,
    SubmitOrderResponse,
    Trade,
    TradeParams,
    BUY,
    SELL,
)
from .exceptions import (
    BlinkApiError,
    BlinkAuthError,
    BlinkError,
    BlinkOrderError,
    BlinkWebSocketError,
    PolyApiException,
)
from .ws import BlinkMarketWs, BlinkPriceWs, BlinkUserWs
from .constants import CONTRACTS, BASE_SEPOLIA_CHAIN_ID

__all__ = [
    # Client
    "ClobClient",
    "BlinkClobClient",
    "AsyncClobClient",
    # Types (Polymarket compat)
    "ApiCreds",
    "BookParams",
    "CreateOrderOptions",
    "MarketOrderArgs",
    "OpenOrderParams",
    "OrderArgs",
    "OrderBookSummary",
    "OrderSummary",
    "OrderType",
    "PartialCreateOrderOptions",
    "PostOrdersArgs",
    "RoundConfig",
    "Side",
    "SignedOrder",
    "TradeParams",
    "BUY",
    "SELL",
    # Types (Blink extras)
    "BalanceAllowance",
    "BalanceAllowanceParams",
    "Market",
    "MatchInfo",
    "OpenOrder",
    "OrderBook",
    "OrderPayload",
    "SignatureType",
    "SubmitOrderResponse",
    "Trade",
    # Exceptions
    "BlinkApiError",
    "BlinkAuthError",
    "BlinkError",
    "BlinkOrderError",
    "BlinkWebSocketError",
    "PolyApiException",
    # WebSocket
    "BlinkMarketWs",
    "BlinkPriceWs",
    "BlinkUserWs",
    # Constants
    "CONTRACTS",
    "BASE_SEPOLIA_CHAIN_ID",
]
