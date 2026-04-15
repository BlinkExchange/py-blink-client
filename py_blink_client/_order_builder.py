# /home/shanmu/Documents/crypto/blink/py-blink-client/py_blink_client/_order_builder.py
"""
Order amount computation and rounding.

Handles conversion from human-readable price/size to raw 6-decimal
makerAmount/takerAmount, with proper financial rounding using Decimal.

Rounding rules (matching Polymarket):
  - Price: rounded DOWN to tick size decimal places
  - Size: rounded DOWN to 2 decimal places
  - Amounts: use ROUNDING_CONFIG[tick_size].amount decimal places

Market price calculation walks the orderbook for FOK/IOC fills.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import List, Optional, Tuple

from .constants import COLLATERAL_SCALE
from .types import (
    MarketOrderArgs,
    OrderArgs,
    OrderBookSummary,
    OrderSummary,
    OrderType,
    RoundConfig,
    SideInt,
)

# Rounding config per tick size
ROUNDING_CONFIG: dict[str, RoundConfig] = {
    "0.1": RoundConfig(price=1, size=2, amount=3),
    "0.01": RoundConfig(price=2, size=2, amount=4),
    "0.001": RoundConfig(price=3, size=2, amount=5),
    "0.0001": RoundConfig(price=4, size=2, amount=6),
}


def _decimal_places(value: float) -> int:
    """Count the number of decimal places in a float."""
    d = Decimal(str(value)).normalize()
    return max(0, -d.as_tuple().exponent)


def round_down(value: float, decimals: int) -> float:
    """Round a value DOWN to the given number of decimal places."""
    d = Decimal(str(value))
    quantize_to = Decimal(10) ** -decimals
    return float(d.quantize(quantize_to, rounding=ROUND_DOWN))


def round_normal(value: float, decimals: int) -> float:
    """Round a value to the given number of decimal places (HALF_UP)."""
    d = Decimal(str(value))
    quantize_to = Decimal(10) ** -decimals
    return float(d.quantize(quantize_to, rounding=ROUND_HALF_UP))


def round_up(value: float, decimals: int) -> float:
    """Round a value UP to the given number of decimal places."""
    from decimal import ROUND_UP as RU
    d = Decimal(str(value))
    quantize_to = Decimal(10) ** -decimals
    return float(d.quantize(quantize_to, rounding=RU))


def to_token_decimals(value: float) -> int:
    """Convert a human-readable value to raw 6-decimal token units."""
    d = Decimal(str(value))
    return int(d * Decimal(str(COLLATERAL_SCALE)))


def build_order_amounts(
    side: str,
    price: float,
    size: float,
    round_config: RoundConfig,
) -> Tuple[int, int, int]:
    """Compute raw makerAmount and takerAmount from price and size.

    Returns:
        Tuple of ``(side_int, maker_amount, taker_amount)`` where amounts
        are in raw 6-decimal token units.
    """
    raw_price = round_down(price, round_config.price)

    if side == "BUY":
        raw_taker_amt = round_down(size, round_config.size)
        raw_maker_amt = raw_taker_amt * raw_price
        if _decimal_places(raw_maker_amt) > round_config.amount:
            raw_maker_amt = round_up(raw_maker_amt, round_config.amount + 4)
            if _decimal_places(raw_maker_amt) > round_config.amount:
                raw_maker_amt = round_down(raw_maker_amt, round_config.amount)

        maker_amount = to_token_decimals(raw_maker_amt)
        taker_amount = to_token_decimals(raw_taker_amt)
        return SideInt.BUY, maker_amount, taker_amount

    elif side == "SELL":
        raw_maker_amt = round_down(size, round_config.size)
        raw_taker_amt = raw_maker_amt * raw_price
        if _decimal_places(raw_taker_amt) > round_config.amount:
            raw_taker_amt = round_up(raw_taker_amt, round_config.amount + 4)
            if _decimal_places(raw_taker_amt) > round_config.amount:
                raw_taker_amt = round_down(raw_taker_amt, round_config.amount)

        maker_amount = to_token_decimals(raw_maker_amt)
        taker_amount = to_token_decimals(raw_taker_amt)
        return SideInt.SELL, maker_amount, taker_amount

    else:
        raise ValueError(f"side must be 'BUY' or 'SELL', got '{side}'")


def build_market_order_amounts(
    side: str,
    amount: float,
    price: float,
    round_config: RoundConfig,
) -> Tuple[int, int, int]:
    """Compute raw amounts for a market order.

    For BUY: ``amount`` is the dollar amount to spend.
    For SELL: ``amount`` is the number of shares to sell.

    Returns:
        Tuple of ``(side_int, maker_amount, taker_amount)``.
    """
    raw_price = round_down(price, round_config.price)

    if side == "BUY":
        raw_maker_amt = round_down(amount, round_config.size)
        raw_taker_amt = raw_maker_amt / raw_price
        if _decimal_places(raw_taker_amt) > round_config.amount:
            raw_taker_amt = round_up(raw_taker_amt, round_config.amount + 4)
            if _decimal_places(raw_taker_amt) > round_config.amount:
                raw_taker_amt = round_down(raw_taker_amt, round_config.amount)

        maker_amount = to_token_decimals(raw_maker_amt)
        taker_amount = to_token_decimals(raw_taker_amt)
        return SideInt.BUY, maker_amount, taker_amount

    elif side == "SELL":
        raw_maker_amt = round_down(amount, round_config.size)
        raw_taker_amt = raw_maker_amt * raw_price
        if _decimal_places(raw_taker_amt) > round_config.amount:
            raw_taker_amt = round_up(raw_taker_amt, round_config.amount + 4)
            if _decimal_places(raw_taker_amt) > round_config.amount:
                raw_taker_amt = round_down(raw_taker_amt, round_config.amount)

        maker_amount = to_token_decimals(raw_maker_amt)
        taker_amount = to_token_decimals(raw_taker_amt)
        return SideInt.SELL, maker_amount, taker_amount

    else:
        raise ValueError(f"side must be 'BUY' or 'SELL', got '{side}'")


def calculate_market_price(
    book: OrderBookSummary,
    side: str,
    amount: float,
    order_type: str = OrderType.FOK,
) -> Optional[float]:
    """Walk the orderbook to find the worst price needed to fill an amount.

    For BUY: walks asks, ``amount`` is dollar amount.
    For SELL: walks bids, ``amount`` is number of shares.

    Args:
        book: OrderBookSummary with bids and asks.
        side: ``"BUY"`` or ``"SELL"``.
        amount: Dollar amount (BUY) or share count (SELL).
        order_type: ``"FOK"`` raises if insufficient liquidity; ``"IOC"`` returns best available.

    Returns:
        Worst fill price, or ``None`` if insufficient liquidity and order_type is not FOK.

    Raises:
        Exception: If ``FOK`` and insufficient liquidity.
    """
    if side == "BUY":
        return _calculate_buy_market_price(book.asks, amount, order_type)
    else:
        return _calculate_sell_market_price(book.bids, amount, order_type)


def _calculate_buy_market_price(
    asks: List[OrderSummary],
    amount_to_match: float,
    order_type: str,
) -> Optional[float]:
    """Walk the ask side to find worst price for a dollar amount."""
    if not asks:
        if order_type == OrderType.FOK:
            raise Exception("no match")
        return None

    # Asks are ascending (best ask = lowest price first).
    # Walk from best ask upward, accumulating dollar volume.
    cumulative = 0.0
    for level in asks:
        cumulative += float(level.size) * float(level.price)
        if cumulative >= amount_to_match:
            return float(level.price)

    if order_type == OrderType.FOK:
        raise Exception("no match")
    return float(asks[-1].price) if asks else None


def _calculate_sell_market_price(
    bids: List[OrderSummary],
    amount_to_match: float,
    order_type: str,
) -> Optional[float]:
    """Walk the bid side to find worst price for a share amount."""
    if not bids:
        if order_type == OrderType.FOK:
            raise Exception("no match")
        return None

    # Bids are descending (best bid = highest price first).
    # Walk from best bid downward, accumulating share volume.
    cumulative = 0.0
    for level in bids:
        cumulative += float(level.size)
        if cumulative >= amount_to_match:
            return float(level.price)

    if order_type == OrderType.FOK:
        raise Exception("no match")
    return float(bids[-1].price) if bids else None
