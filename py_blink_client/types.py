"""Dataclasses and payload structs for the Blink CLOB client."""
from __future__ import annotations

import msgspec
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Side -- str enum so "BUY" == Side.BUY is True
# ---------------------------------------------------------------------------

class Side(str):
    """Order side. Both ``Side.BUY`` and plain ``"BUY"`` work everywhere."""
    BUY = "BUY"
    SELL = "SELL"

    def __new__(cls, value: str = "BUY"):
        obj = str.__new__(cls, value)
        return obj


# Re-export as module-level constants (Polymarket compat)
BUY = "BUY"
SELL = "SELL"


class SideInt(IntEnum):
    """On-chain side encoding (uint8)."""
    BUY = 0
    SELL = 1


# ---------------------------------------------------------------------------
# OrderType -- plain class with string attrs (Polymarket compat)
# ---------------------------------------------------------------------------

class OrderType:
    """Time-in-force / order type.

    Plain class with string attributes matching Polymarket's ``OrderType``.
    """
    GTC = "GTC"
    FOK = "FOK"
    GTD = "GTD"
    IOC = "IOC"
    FAK = "IOC"  # Polymarket compat alias -- backend uses IOC


class SignatureType(IntEnum):
    """On-chain signature type encoding (uint8)."""
    EOA = 0
    POLY_1271 = 3


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ApiCreds:
    """HMAC API credentials.

    Field names match Polymarket's ``py-clob-client``.
    """
    api_key: str
    api_secret: str
    api_passphrase: str

    def __repr__(self) -> str:
        return f"ApiCreds(api_key='{self.api_key[:8]}...', api_secret='***', api_passphrase='***')"

    # Convenience aliases (TS SDK uses these names)
    @property
    def secret(self) -> str:
        return self.api_secret

    @property
    def passphrase(self) -> str:
        return self.api_passphrase

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ApiCreds":
        return cls(
            api_key=d.get("api_key", d.get("apiKey", "")),
            api_secret=d.get("api_secret", d.get("secret", "")),
            api_passphrase=d.get("api_passphrase", d.get("passphrase", "")),
        )


# ---------------------------------------------------------------------------
# Order args (user-facing, human units) -- Polymarket-compatible
# ---------------------------------------------------------------------------

@dataclass
class OrderArgs:
    """Arguments for creating an order (human-readable units).

    Matches Polymarket's ``OrderArgs`` signature. ``price`` is in the range
    (0, 1) for prediction markets. ``size`` is the number of outcome tokens.
    ``side`` is a plain string (``"BUY"`` or ``"SELL"``).
    """
    token_id: str = ""
    price: float = 0.0
    size: float = 0.0
    side: str = BUY
    fee_rate_bps: int = 0
    nonce: int = 0
    expiration: int = 0
    taker: str = "0x0000000000000000000000000000000000000000"

    def __post_init__(self) -> None:
        self.side = str(self.side).upper()
        self.validate()

    def validate(self) -> None:
        """Validate order arguments, raising ``ValueError`` on bad input."""
        if not self.token_id:
            raise ValueError("token_id is required")
        if not (0 < self.price < 1):
            raise ValueError(f"price must be between 0 and 1 (exclusive), got {self.price}")
        if self.size <= 0:
            raise ValueError(f"size must be positive, got {self.size}")
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side must be 'BUY' or 'SELL', got '{self.side}'")


@dataclass
class MarketOrderArgs:
    """Arguments for creating a market order (Polymarket compat)."""
    token_id: str = ""
    amount: float = 0.0
    side: str = BUY
    price: float = 0.0
    fee_rate_bps: int = 0
    nonce: int = 0
    taker: str = "0x0000000000000000000000000000000000000000"
    order_type: str = "FOK"  # FOK or IOC -- controls liquidity matching

    def __post_init__(self) -> None:
        self.side = str(self.side).upper()
        if not self.token_id:
            raise ValueError("token_id is required")
        if self.amount <= 0:
            raise ValueError(f"amount must be positive, got {self.amount}")
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"side must be 'BUY' or 'SELL', got '{self.side}'")


@dataclass
class CreateOrderOptions:
    """Order creation options (Polymarket compat)."""
    tick_size: str = "0.01"
    neg_risk: bool = False


@dataclass
class PartialCreateOrderOptions:
    """Optional order creation options (Polymarket compat).

    Blink ignores ``neg_risk`` -- all Blink markets are standard risk.
    Accepted without error for Polymarket bot compatibility.
    """
    tick_size: Optional[str] = None
    neg_risk: Optional[bool] = None


@dataclass
class RoundConfig:
    """Rounding configuration for a specific tick size."""
    price: int   # decimal places to round price
    size: int    # decimal places to round size
    amount: int  # decimal places to round maker/taker amounts


# ---------------------------------------------------------------------------
# Query parameter types (Polymarket compat)
# ---------------------------------------------------------------------------

@dataclass
class BookParams:
    """Parameters for batch orderbook requests."""
    token_id: str = ""
    side: str = ""


@dataclass
class OpenOrderParams:
    """Filter parameters for ``get_orders()`` (Polymarket compat)."""
    id: Optional[str] = None
    market: Optional[str] = None
    asset_id: Optional[str] = None

    def to_dict(self) -> Dict[str, str]:
        d: Dict[str, str] = {}
        if self.id is not None:
            d["id"] = self.id
        if self.market is not None:
            d["market_id"] = self.market
        if self.asset_id is not None:
            d["asset_id"] = self.asset_id
        return d


@dataclass
class TradeParams:
    """Filter parameters for ``get_trades()`` (Polymarket compat)."""
    id: Optional[str] = None
    maker_address: Optional[str] = None
    market: Optional[str] = None
    asset_id: Optional[str] = None
    before: Optional[int] = None
    after: Optional[int] = None

    def to_dict(self) -> Dict[str, str]:
        d: Dict[str, str] = {}
        if self.id is not None:
            d["id"] = self.id
        if self.maker_address is not None:
            d["maker"] = self.maker_address
        if self.market is not None:
            d["market_id"] = self.market
        if self.asset_id is not None:
            d["asset_id"] = self.asset_id
        if self.before is not None:
            d["before"] = self.before
        if self.after is not None:
            d["after"] = self.after
        return d


@dataclass
class BalanceAllowanceParams:
    """Parameters for balance/allowance queries."""
    asset_type: str = "COLLATERAL"  # COLLATERAL or CONDITIONAL
    token_id: Optional[str] = None


@dataclass
class PostOrdersArgs:
    """Arguments for batch order submission (Polymarket compat)."""
    order: Dict[str, Any] = field(default_factory=dict)
    orderType: str = "GTC"
    postOnly: bool = False


# ---------------------------------------------------------------------------
# Hot-path types -- msgspec.Struct for fast serialization
# ---------------------------------------------------------------------------

class SignedOrderPayload(msgspec.Struct):
    """Signed order payload for POST /order -- msgspec for fast encode.

    This is the ``order`` nested object inside the SubmitOrderRequest.
    Field names use camelCase to match the wire format directly.
    """
    salt: str
    maker: str
    signer: str
    taker: str
    tokenId: str
    makerAmount: str
    takerAmount: str
    expiration: str
    nonce: str
    feeRateBps: str
    side: str           # "BUY" or "SELL" string
    signatureType: int  # 0=EOA, 3=POLY_1271
    signature: str


class SubmitOrderRequest(msgspec.Struct):
    """Full order submission request -- msgspec for fast encode."""
    order: SignedOrderPayload
    owner: str
    order_type: str = "GTC"
    post_only: bool = False


# ---------------------------------------------------------------------------
# Signed order (legacy dataclass for API compat)
# ---------------------------------------------------------------------------

@dataclass
class SignedOrder:
    """A signed order ready to be submitted to the backend."""
    salt: str = ""
    maker: str = ""
    signer: str = ""
    taker: str = ""
    token_id: str = ""
    maker_amount: str = ""
    taker_amount: str = ""
    expiration: str = ""
    nonce: str = ""
    fee_rate_bps: str = ""
    side: int = 0  # SideInt value
    signature_type: int = 0  # SignatureType value
    signature: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Convert to camelCase dict (Polymarket wire format)."""
        side_str = "BUY" if self.side == SideInt.BUY else "SELL"
        return {
            "salt": self.salt,
            "maker": self.maker,
            "signer": self.signer,
            "taker": self.taker,
            "tokenId": self.token_id,
            "makerAmount": self.maker_amount,
            "takerAmount": self.taker_amount,
            "expiration": self.expiration,
            "nonce": self.nonce,
            "feeRateBps": self.fee_rate_bps,
            "side": side_str,
            "signatureType": int(self.signature_type),
            "signature": self.signature,
        }

    def to_msgspec_payload(self) -> SignedOrderPayload:
        """Convert to msgspec Struct for fast serialization."""
        side_str = "BUY" if self.side == SideInt.BUY else "SELL"
        return SignedOrderPayload(
            salt=self.salt,
            maker=self.maker,
            signer=self.signer,
            taker=self.taker,
            tokenId=self.token_id,
            makerAmount=self.maker_amount,
            takerAmount=self.taker_amount,
            expiration=self.expiration,
            nonce=self.nonce,
            feeRateBps=self.fee_rate_bps,
            side=side_str,
            signatureType=int(self.signature_type),
            signature=self.signature,
        )


@dataclass
class OrderPayload:
    """JSON payload for POST /order."""
    order: Dict[str, Any] = field(default_factory=dict)
    owner: str = ""
    order_type: str = "GTC"
    deferExec: bool = False
    post_only: Optional[bool] = None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "order": self.order,
            "owner": self.owner,
            "order_type": self.order_type,
        }
        if self.post_only is not None:
            d["post_only"] = self.post_only
        return d


# ---------------------------------------------------------------------------
# API response types
# ---------------------------------------------------------------------------

@dataclass
class Market:
    """Simplified market representation."""
    id: str = ""
    symbol: str = ""
    condition_id: str = ""
    yes_token_id: str = ""
    no_token_id: str = ""
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Market":
        return cls(
            id=d.get("id", ""),
            symbol=d.get("symbol", ""),
            condition_id=d.get("condition_id", d.get("conditionId", "")),
            yes_token_id=d.get("yes_token_id", d.get("yesTokenId", "")),
            no_token_id=d.get("no_token_id", d.get("noTokenId", "")),
            status=d.get("status", ""),
            raw=d,
        )


@dataclass
class OrderSummary:
    """A single price/size level in an orderbook."""
    price: str = ""
    size: str = ""


# Backward compat alias
OrderBookLevel = OrderSummary


@dataclass
class OrderBookSummary:
    """Orderbook snapshot (Polymarket-compatible name and fields)."""
    market: str = ""
    asset_id: str = ""
    timestamp: str = ""
    bids: List[OrderSummary] = field(default_factory=list)
    asks: List[OrderSummary] = field(default_factory=list)
    hash: str = ""
    min_order_size: str = ""
    tick_size: str = ""
    neg_risk: bool = False
    last_trade_price: str = ""

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OrderBookSummary":
        return cls(
            market=d.get("market", d.get("market_id", "")),
            asset_id=d.get("asset_id", ""),
            timestamp=d.get("timestamp", ""),
            bids=[
                OrderSummary(price=str(b.get("price", "0")), size=str(b.get("size", "0")))
                for b in d.get("bids", [])
            ],
            asks=[
                OrderSummary(price=str(a.get("price", "0")), size=str(a.get("size", "0")))
                for a in d.get("asks", [])
            ],
            hash=d.get("hash", ""),
            min_order_size=d.get("min_order_size", ""),
            tick_size=d.get("tick_size", ""),
            neg_risk=d.get("neg_risk", False),
            last_trade_price=d.get("last_trade_price", ""),
        )


# Backward compat alias
OrderBook = OrderBookSummary


@dataclass
class OpenOrder:
    """An open order from GET /data/orders."""
    order_id: str = ""
    market_id: str = ""
    asset_id: str = ""
    side: str = ""
    price: str = ""
    size: str = ""
    status: str = ""
    size_remaining: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "OpenOrder":
        return cls(
            order_id=d.get("order_id", d.get("id", "")),
            market_id=d.get("market_id", ""),
            asset_id=d.get("asset_id", d.get("token_id", "")),
            side=d.get("side", ""),
            price=str(d.get("price", "0")),
            size=str(d.get("size", "0")),
            status=d.get("status", ""),
            size_remaining=str(d.get("size_remaining", "0")),
            raw=d,
        )


@dataclass
class Trade:
    """A trade from GET /data/trades."""
    trade_id: str = ""
    market_id: str = ""
    asset_id: str = ""
    side: str = ""
    price: str = ""
    size: str = ""
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Trade":
        return cls(
            trade_id=d.get("trade_id", d.get("id", "")),
            market_id=d.get("market_id", ""),
            asset_id=d.get("asset_id", d.get("token_id", "")),
            side=d.get("side", ""),
            price=str(d.get("price", "0")),
            size=str(d.get("size", "0")),
            status=d.get("status", ""),
            raw=d,
        )


@dataclass
class BalanceAllowance:
    """Response from GET /balance-allowance."""
    balance: str = ""
    allowance: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "BalanceAllowance":
        return cls(
            balance=str(d.get("balance", "0")),
            allowance=str(d.get("allowance", "0")),
            raw=d,
        )
