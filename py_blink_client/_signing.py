"""EIP-712 order signing with a cached domain separator."""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

from .constants import (
    ORDER_DOMAIN_NAME,
    ORDER_DOMAIN_VERSION,
    ORDER_EIP712_TYPES,
)

logger = logging.getLogger(__name__)


def _parse_token_id(value: Any) -> int:
    """Parse a token ID from any common format into a Python int.

    Handles:
      - ``int`` pass-through
      - ``"0x1a2b..."`` -- hex string with prefix
      - ``"1a2b..."`` -- hex string without prefix (contains ``a``-``f``)
      - ``"123456789"`` -- pure decimal string

    The 64-char all-digit ambiguity: uint256 hex representation is exactly
    64 chars; decimal would be <=78 digits but 64-digit decimals are
    astronomically unlikely token IDs, so we treat them as hex.

    Returns:
        The token ID as a Python ``int``.
    """
    if isinstance(value, int):
        return value

    s = str(value).strip()

    # Hex with explicit 0x prefix
    if s.startswith("0x") or s.startswith("0X"):
        return int(s, 16)

    # Contains hex digits a-f -> treat as hex without prefix
    if any(c in "abcdefABCDEF" for c in s):
        return int(s, 16)

    # 64-char all-digit string -- ambiguous but practically always hex
    if len(s) == 64 and s.isdigit():
        return int(s, 16)

    # Pure decimal digits
    return int(s)


class BlinkSigner:
    """EIP-712 order signer. Caches the Account and domain separator."""

    def __init__(
        self,
        private_key: str,
        chain_id: int,
        exchange_address: str,
    ) -> None:
        self._account = Account.from_key(private_key)
        self._chain_id = chain_id
        self._exchange_address = exchange_address

        # Pre-compute and cache the domain data dict
        self._domain_data = {
            "name": ORDER_DOMAIN_NAME,
            "version": ORDER_DOMAIN_VERSION,
            "chainId": chain_id,
            "verifyingContract": exchange_address,
        }

    @property
    def address(self) -> str:
        """Return the signer's Ethereum address."""
        return self._account.address

    @property
    def account(self) -> Any:
        """Return the cached Account object."""
        return self._account

    def sign_order(self, order_data: Dict[str, Any]) -> str:
        """Sign an order via EIP-712 and return the hex signature.

        Args:
            order_data: Order fields matching the Order struct. All field values
                can be strings or ints -- they will be coerced to the correct types.

        Returns:
            Hex-encoded EIP-712 signature (``0x``-prefixed).
        """
        # Ensure all numeric fields are ints for the encoder
        message_data = {
            "salt": int(order_data["salt"]),
            "maker": order_data["maker"],
            "signer": order_data["signer"],
            "taker": order_data["taker"],
            "tokenId": _parse_token_id(order_data["tokenId"]),
            "makerAmount": int(order_data["makerAmount"]),
            "takerAmount": int(order_data["takerAmount"]),
            "expiration": int(order_data["expiration"]),
            "nonce": int(order_data["nonce"]),
            "feeRateBps": int(order_data["feeRateBps"]),
            "side": int(order_data["side"]),
            "signatureType": int(order_data["signatureType"]),
        }

        signable = encode_typed_data(
            self._domain_data,
            ORDER_EIP712_TYPES,
            message_data,
        )
        signed = self._account.sign_message(signable)
        sig_hex = signed.signature.hex()
        return f"0x{sig_hex}" if not sig_hex.startswith("0x") else sig_hex
