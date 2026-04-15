# /home/shanmu/Documents/crypto/blink/py-blink-client/py_blink_client/_auth.py
"""
Authentication helpers for the Blink CLOB client.

Two auth layers:
  - L1 (EIP-712): Used for API-key creation/derivation. Signs a typed-data
    message with the wallet's private key.
  - L2 (HMAC-SHA256): Used for all authenticated REST calls after an API key
    has been obtained. Signs ``timestamp + method + path + body`` with the
    base64-encoded API secret.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import Any, Dict, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

from .constants import (
    AUTH_DOMAIN_NAME,
    AUTH_DOMAIN_VERSION,
    AUTH_EIP712_TYPES,
    AUTH_MSG_TO_SIGN,
)
from .exceptions import BlinkAuthError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# L2 -- HMAC authentication (for normal authenticated requests)
# ---------------------------------------------------------------------------


def build_hmac_signature(
    secret: str,
    timestamp: int,
    method: str,
    path: str,
    body: str = "",
    secret_bytes: Optional[bytes] = None,
) -> str:
    """Build the HMAC-SHA256 signature for an authenticated request.

    The ``secret`` is a URL-safe base64-encoded string from the backend.
    We decode it (with padding fix), HMAC-sign the message, and return
    the result as URL-safe base64.

    Args:
        secret: The API secret (URL-safe base64 encoded).
        timestamp: Unix epoch seconds.
        method: HTTP method (``GET``, ``POST``, ``DELETE``).
        path: Request path (e.g. ``/order``). NO query string.
        body: Request body string (empty for GET/DELETE without body).
        secret_bytes: Pre-decoded secret bytes (avoids base64 decode per call).

    Returns:
        URL-safe base64-encoded HMAC signature.
    """
    message = f"{timestamp}{method}{path}{body}"
    if secret_bytes is None:
        # Pad to valid base64 length (must be multiple of 4)
        secret_padded = secret + "=" * (-len(secret) % 4)
        secret_bytes = urlsafe_b64decode(secret_padded)
    sig_bytes = hmac.new(secret_bytes, message.encode(), hashlib.sha256).digest()
    return urlsafe_b64encode(sig_bytes).decode()


def create_l2_headers(
    api_key: str,
    secret: str,
    passphrase: str,
    address: str,
    method: str,
    path: str,
    body: str = "",
    timestamp: Optional[int] = None,
    secret_bytes: Optional[bytes] = None,
) -> Dict[str, str]:
    """Create L2 (HMAC) authentication headers.

    Returns:
        Dict with ``BLINK-*`` header keys.
    """
    ts = timestamp if timestamp is not None else int(time.time())
    sig = build_hmac_signature(secret, ts, method, path, body, secret_bytes=secret_bytes)
    return {
        "BLINK-ADDRESS": address,
        "BLINK-API-KEY": api_key,
        "BLINK-SIGNATURE": sig,
        "BLINK-TIMESTAMP": str(ts),
        "BLINK-PASSPHRASE": passphrase,
    }


def create_l2_headers_fast(
    static_headers: Dict[str, str],
    secret_bytes: bytes,
    method: str,
    path: str,
    body: str = "",
) -> Dict[str, str]:
    """Create L2 headers using pre-computed static parts (hot-path optimization).

    ``static_headers`` should contain BLINK-ADDRESS, BLINK-API-KEY, BLINK-PASSPHRASE.
    Only BLINK-SIGNATURE and BLINK-TIMESTAMP are computed per-request.
    """
    ts = int(time.time())
    message = f"{ts}{method}{path}{body}"
    sig_bytes = hmac.new(secret_bytes, message.encode(), hashlib.sha256).digest()
    sig = urlsafe_b64encode(sig_bytes).decode()
    return {
        **static_headers,
        "BLINK-SIGNATURE": sig,
        "BLINK-TIMESTAMP": str(ts),
    }


# ---------------------------------------------------------------------------
# L1 -- EIP-712 authentication (for API-key management)
# ---------------------------------------------------------------------------


def build_eip712_auth_signature(
    private_key: str,
    chain_id: int,
    timestamp: Optional[int] = None,
    nonce: int = 0,
    address_override: Optional[str] = None,
    account: Optional[Any] = None,
) -> tuple[str, str, int]:
    """Sign the ClobAuth EIP-712 message for L1 authentication.

    Args:
        private_key: Hex-encoded private key (with or without ``0x`` prefix).
        chain_id: EVM chain ID.
        timestamp: Unix epoch seconds (defaults to now).
        nonce: Auth nonce (usually 0).
        address_override: Override the derived address (for smart wallets).
        account: Pre-derived Account object (avoids re-deriving EC key).

    Returns:
        Tuple of ``(signature_hex, address, timestamp)``.
    """
    if account is None:
        account = Account.from_key(private_key)
    address = address_override or account.address
    ts = timestamp if timestamp is not None else int(time.time())

    domain_data = {
        "name": AUTH_DOMAIN_NAME,
        "version": AUTH_DOMAIN_VERSION,
        "chainId": chain_id,
    }

    message_data = {
        "address": address,
        "timestamp": str(ts),
        "nonce": nonce,
        "message": AUTH_MSG_TO_SIGN,
    }

    signable = encode_typed_data(
        domain_data,
        AUTH_EIP712_TYPES,
        message_data,
    )
    signed = account.sign_message(signable)
    return signed.signature.hex(), address, ts


def create_l1_headers(
    private_key: str,
    chain_id: int,
    nonce: int = 0,
    timestamp: Optional[int] = None,
    address_override: Optional[str] = None,
    account: Optional[Any] = None,
) -> Dict[str, str]:
    """Create L1 (EIP-712) authentication headers.

    Returns:
        Dict with ``BLINK-*`` header keys.
    """
    sig_hex, address, ts = build_eip712_auth_signature(
        private_key, chain_id, timestamp, nonce, address_override, account=account,
    )
    return {
        "BLINK-ADDRESS": address,
        "BLINK-SIGNATURE": f"0x{sig_hex}" if not sig_hex.startswith("0x") else sig_hex,
        "BLINK-TIMESTAMP": str(ts),
        "BLINK-NONCE": str(nonce),
    }
