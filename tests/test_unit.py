# /home/shanmu/Documents/crypto/blink/py-blink-client/tests/test_unit.py
"""
Unit tests for py-blink-client v2.

Tests auth, signing, order builder, types, stubs, and hot path without
requiring a running backend.
"""
import hashlib
import json
import os
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from unittest.mock import patch, MagicMock

import pytest

from py_blink_client._auth import (
    build_hmac_signature,
    create_l2_headers,
    create_l2_headers_fast,
    build_eip712_auth_signature,
    create_l1_headers,
)
from py_blink_client._signing import BlinkSigner, _parse_token_id
from py_blink_client._order_builder import (
    build_order_amounts,
    build_market_order_amounts,
    round_down,
    round_normal,
    to_token_decimals,
    ROUNDING_CONFIG,
    calculate_market_price,
)
from py_blink_client.types import (
    ApiCreds,
    OrderArgs,
    MarketOrderArgs,
    OrderType,
    Side,
    SideInt,
    SignatureType,
    OrderBookSummary,
    OrderSummary,
    SignedOrderPayload,
    SubmitOrderRequest,
    BookParams,
    OpenOrderParams,
    TradeParams,
    PostOrdersArgs,
    RoundConfig,
    BalanceAllowanceParams,
)
from py_blink_client.constants import (
    CONTRACTS,
    COLLATERAL_SCALE,
    END_CURSOR,
    ZERO_ADDRESS,
    ORDER_DOMAIN_NAME,
    BASE_SEPOLIA_CHAIN_ID,
)
from py_blink_client.exceptions import (
    BlinkApiError,
    BlinkAuthError,
    BlinkOrderError,
    BlinkWebSocketError,
    PolyApiException,
)
from py_blink_client.client import ClobClient


# ---------------------------------------------------------------------------
# Auth tests
# ---------------------------------------------------------------------------

class TestHmacAuth:
    def test_hmac_signature_deterministic(self):
        raw_secret = os.urandom(32)
        secret_b64 = urlsafe_b64encode(raw_secret).decode().rstrip("=")
        sig1 = build_hmac_signature(secret_b64, 1000, "GET", "/orders")
        sig2 = build_hmac_signature(secret_b64, 1000, "GET", "/orders")
        assert sig1 == sig2

    def test_hmac_secret_bytes_optimization(self):
        raw_secret = os.urandom(32)
        secret_b64 = urlsafe_b64encode(raw_secret).decode().rstrip("=")
        sig1 = build_hmac_signature(secret_b64, 1000, "POST", "/order", '{"test":1}')
        sig2 = build_hmac_signature(secret_b64, 1000, "POST", "/order", '{"test":1}', secret_bytes=raw_secret)
        assert sig1 == sig2

    def test_hmac_different_timestamps(self):
        raw_secret = os.urandom(32)
        secret_b64 = urlsafe_b64encode(raw_secret).decode().rstrip("=")
        sig1 = build_hmac_signature(secret_b64, 1000, "GET", "/orders")
        sig2 = build_hmac_signature(secret_b64, 1001, "GET", "/orders")
        assert sig1 != sig2

    def test_l2_headers_structure(self):
        headers = create_l2_headers(
            api_key="test-key", secret="dGVzdA", passphrase="pass",
            address="0xabc", method="GET", path="/orders",
        )
        assert "BLINK-ADDRESS" in headers
        assert "BLINK-API-KEY" in headers
        assert "BLINK-SIGNATURE" in headers
        assert "BLINK-TIMESTAMP" in headers
        assert "BLINK-PASSPHRASE" in headers
        assert headers["BLINK-ADDRESS"] == "0xabc"
        assert headers["BLINK-API-KEY"] == "test-key"

    def test_l2_headers_fast(self):
        raw_secret = os.urandom(32)
        static = {"BLINK-ADDRESS": "0xabc", "BLINK-API-KEY": "key", "BLINK-PASSPHRASE": "pass"}
        headers = create_l2_headers_fast(static, raw_secret, "POST", "/order", '{"x":1}')
        assert headers["BLINK-ADDRESS"] == "0xabc"
        assert "BLINK-SIGNATURE" in headers
        assert "BLINK-TIMESTAMP" in headers


class TestEip712Auth:
    def test_l1_headers_structure(self):
        key = "0x" + "ab" * 32
        headers = create_l1_headers(key, 84532)
        assert "BLINK-ADDRESS" in headers
        assert "BLINK-SIGNATURE" in headers
        assert "BLINK-TIMESTAMP" in headers
        assert "BLINK-NONCE" in headers
        assert headers["BLINK-SIGNATURE"].startswith("0x")


# ---------------------------------------------------------------------------
# Signing tests
# ---------------------------------------------------------------------------

class TestTokenIdParsing:
    def test_int_passthrough(self):
        assert _parse_token_id(123) == 123

    def test_hex_with_prefix(self):
        assert _parse_token_id("0xff") == 255

    def test_hex_without_prefix(self):
        assert _parse_token_id("ff") == 255

    def test_decimal_string(self):
        assert _parse_token_id("100") == 100

    def test_64_char_all_digits_treated_as_hex(self):
        assert _parse_token_id("0" * 64) == 0


class TestBlinkSigner:
    def setup_method(self):
        self.key = "0x" + "ab" * 32
        self.signer = BlinkSigner(self.key, 84532, CONTRACTS["exchange"])

    def test_address(self):
        assert self.signer.address.startswith("0x")
        assert len(self.signer.address) == 42

    def test_sign_order(self):
        order = {
            "salt": "12345", "maker": self.signer.address,
            "signer": self.signer.address,
            "taker": ZERO_ADDRESS, "tokenId": "999",
            "makerAmount": "1000000", "takerAmount": "500000",
            "expiration": "0", "nonce": "0", "feeRateBps": "0",
            "side": 0, "signatureType": 0,
        }
        sig = self.signer.sign_order(order)
        assert sig.startswith("0x")
        assert len(sig) > 100

    def test_sign_deterministic(self):
        order = {
            "salt": "99999", "maker": self.signer.address,
            "signer": self.signer.address,
            "taker": ZERO_ADDRESS, "tokenId": "1",
            "makerAmount": "100", "takerAmount": "50",
            "expiration": "0", "nonce": "0", "feeRateBps": "0",
            "side": 0, "signatureType": 0,
        }
        sig1 = self.signer.sign_order(order)
        sig2 = self.signer.sign_order(order)
        assert sig1 == sig2


# ---------------------------------------------------------------------------
# Order builder tests
# ---------------------------------------------------------------------------

class TestOrderBuilder:
    def test_round_down(self):
        assert round_down(1.999, 2) == 1.99
        assert round_down(1.001, 2) == 1.00

    def test_round_normal(self):
        assert round_normal(0.555, 2) == 0.56
        assert round_normal(0.554, 2) == 0.55

    def test_to_token_decimals(self):
        assert to_token_decimals(1.5) == 1_500_000
        assert to_token_decimals(0.01) == 10_000

    def test_buy_order_amounts_01(self):
        rc = ROUNDING_CONFIG["0.1"]
        side_int, maker, taker = build_order_amounts("BUY", 0.5, 10.0, rc)
        assert side_int == SideInt.BUY
        assert taker == 10_000_000
        assert maker == 5_000_000

    def test_sell_order_amounts_001(self):
        rc = ROUNDING_CONFIG["0.01"]
        side_int, maker, taker = build_order_amounts("SELL", 0.55, 10.0, rc)
        assert side_int == SideInt.SELL
        assert maker == 10_000_000
        assert taker == 5_500_000

    def test_buy_at_each_tick_size(self):
        for tick, rc in ROUNDING_CONFIG.items():
            side_int, maker, taker = build_order_amounts("BUY", 0.5, 10.0, rc)
            assert side_int == SideInt.BUY
            assert maker > 0
            assert taker > 0

    def test_market_order_amounts_buy(self):
        rc = ROUNDING_CONFIG["0.01"]
        side_int, maker, taker = build_market_order_amounts("BUY", 5.0, 0.5, rc)
        assert side_int == SideInt.BUY
        assert maker == 5_000_000  # $5 USDC

    def test_invalid_side_raises(self):
        rc = ROUNDING_CONFIG["0.01"]
        with pytest.raises(ValueError, match="side must be"):
            build_order_amounts("HOLD", 0.5, 10.0, rc)


class TestMarketPriceCalculation:
    def test_buy_market_price(self):
        book = OrderBookSummary(
            asks=[OrderSummary(price="0.50", size="10"), OrderSummary(price="0.60", size="5")],
        )
        price = calculate_market_price(book, "BUY", 5.0, OrderType.FOK)
        assert price is not None

    def test_sell_market_price(self):
        book = OrderBookSummary(
            bids=[OrderSummary(price="0.40", size="8"), OrderSummary(price="0.30", size="12")],
        )
        price = calculate_market_price(book, "SELL", 5.0, OrderType.FOK)
        assert price is not None

    def test_fok_raises_on_no_liquidity(self):
        book = OrderBookSummary(asks=[], bids=[])
        with pytest.raises(Exception, match="no match"):
            calculate_market_price(book, "BUY", 100.0, OrderType.FOK)

    def test_ioc_returns_best(self):
        book = OrderBookSummary(
            asks=[OrderSummary(price="0.50", size="1")],
        )
        price = calculate_market_price(book, "BUY", 100.0, OrderType.IOC)
        assert price == 0.5


# ---------------------------------------------------------------------------
# Types tests
# ---------------------------------------------------------------------------

class TestTypes:
    def test_order_args_validation(self):
        with pytest.raises(ValueError, match="token_id is required"):
            OrderArgs()
        with pytest.raises(ValueError, match="price must be between"):
            OrderArgs(token_id="abc", price=1.5, size=10)
        with pytest.raises(ValueError, match="size must be positive"):
            OrderArgs(token_id="abc", price=0.5, size=-1)

    def test_order_type_fak_alias(self):
        assert OrderType.FAK == OrderType.IOC

    def test_api_creds_from_dict_snake(self):
        creds = ApiCreds.from_dict({"api_key": "k", "secret": "s", "passphrase": "p"})
        assert creds.api_key == "k"
        assert creds.api_secret == "s"

    def test_api_creds_from_dict_camel(self):
        creds = ApiCreds.from_dict({"apiKey": "k", "secret": "s", "passphrase": "p"})
        assert creds.api_key == "k"

    def test_msgspec_encode(self):
        import msgspec
        payload = SignedOrderPayload(
            salt="1", maker="0x1", signer="0x1", taker="0x0",
            tokenId="2", makerAmount="100", takerAmount="50",
            expiration="0", nonce="0", feeRateBps="0", side="BUY",
            signatureType=0, signature="0xabc",
        )
        encoded = msgspec.json.encode(payload)
        assert b"salt" in encoded
        assert b"makerAmount" in encoded


# ---------------------------------------------------------------------------
# Stubs tests
# ---------------------------------------------------------------------------

class TestPolymarketStubs:
    def setup_method(self):
        self.client = ClobClient("https://api.blink15.com")

    def teardown_method(self):
        self.client.close()

    def test_get_notifications_raises(self):
        with pytest.raises(NotImplementedError, match="Polymarket"):
            self.client.get_notifications()

    def test_is_order_scoring_raises(self):
        with pytest.raises(NotImplementedError, match="Polymarket"):
            self.client.is_order_scoring()

    def test_rfq_stub(self):
        with pytest.raises(NotImplementedError, match="Polymarket"):
            self.client.rfq.create_rfq()

    def test_get_closed_only_mode_raises(self):
        with pytest.raises(NotImplementedError, match="Polymarket"):
            self.client.get_closed_only_mode()

    def test_polymarket_compat_redirects(self):
        # These should NOT raise -- they delegate to get_markets
        # (but will fail with network error since no backend)
        # Just verify they exist and are callable
        assert hasattr(self.client, "get_simplified_markets")
        assert hasattr(self.client, "get_sampling_markets")
        assert hasattr(self.client, "get_sampling_simplified_markets")


# ---------------------------------------------------------------------------
# warm_cache tests
# ---------------------------------------------------------------------------

class TestWarmCache:
    def setup_method(self):
        self.client = ClobClient("http://localhost:9999", key="0x" + "ab" * 32)

    def teardown_method(self):
        self.client.close()

    def test_warm_cache_populates_all_caches(self):
        """warm_cache should populate tick_size, fee_rate, and neg_risk caches."""
        tokens = ["tok1", "tok2"]

        def mock_get_tick_size(token_id):
            self.client._tick_size_cache[token_id] = "0.01"
            return {"minimum_tick_size": "0.01"}

        def mock_get_fee_rate(token_id):
            self.client._fee_rate_cache[token_id] = 0
            return {"base_fee": 0}

        def mock_get_neg_risk(token_id):
            self.client._neg_risk_cache[token_id] = False
            return {"neg_risk": False}

        with patch.object(ClobClient, "get_tick_size", side_effect=mock_get_tick_size) as mock_tick, \
             patch.object(ClobClient, "get_fee_rate_bps", side_effect=mock_get_fee_rate) as mock_fee, \
             patch.object(ClobClient, "get_neg_risk", side_effect=mock_get_neg_risk) as mock_neg:
            self.client.warm_cache(tokens)
            # Verify tick size cache populated
            assert "tok1" in self.client._tick_size_cache
            assert "tok2" in self.client._tick_size_cache
            # Verify fee rate cache populated
            assert "tok1" in self.client._fee_rate_cache
            assert "tok2" in self.client._fee_rate_cache
            # Verify neg risk cache populated
            assert "tok1" in self.client._neg_risk_cache
            assert "tok2" in self.client._neg_risk_cache
            # Verify concurrent execution: should have been called for each token
            assert mock_tick.call_count == 2
            assert mock_fee.call_count == 2
            assert mock_neg.call_count == 2


# ---------------------------------------------------------------------------
# Exception tests
# ---------------------------------------------------------------------------

class TestExceptions:
    def test_poly_compat_alias(self):
        try:
            raise BlinkApiError(404, "not found", "GET", "/orders")
        except PolyApiException as e:
            assert e.status_code == 404

    def test_hierarchy(self):
        from py_blink_client.exceptions import BlinkError
        assert issubclass(BlinkApiError, BlinkError)
        assert issubclass(BlinkAuthError, BlinkError)
        assert issubclass(BlinkOrderError, BlinkError)
        assert issubclass(BlinkWebSocketError, BlinkError)


# ---------------------------------------------------------------------------
# Client core tests
# ---------------------------------------------------------------------------

class TestClientCore:
    def test_l0_creation(self):
        c = ClobClient("https://api.blink15.com")
        assert c.get_address() is None
        assert c.get_collateral_address() == CONTRACTS["usdc"]
        assert c.get_exchange_address() == CONTRACTS["exchange"]
        assert c.get_conditional_address() == CONTRACTS["ctf"]
        c.close()

    def test_l1_creation(self):
        key = "0x" + "ab" * 32
        c = ClobClient("https://api.blink15.com", key=key)
        assert c.address.startswith("0x")
        assert len(c.address) == 42
        c.close()

    def test_invalid_host(self):
        with pytest.raises(ValueError, match="host must be"):
            ClobClient("not-a-url")

    def test_invalid_key_length(self):
        with pytest.raises(ValueError, match="Private key must be"):
            ClobClient("https://api.blink15.com", key="0x123")

    def test_set_api_creds(self):
        key = "0x" + "ab" * 32
        c = ClobClient("https://api.blink15.com", key=key)
        creds = ApiCreds(api_key="k", api_secret="dGVzdA", api_passphrase="p")
        c.set_api_creds(creds)
        assert c.creds is creds
        assert c._secret_bytes is not None
        assert c._static_l2_headers is not None
        c.close()

    def test_context_manager(self):
        with ClobClient("https://api.blink15.com") as c:
            assert c.host == "https://api.blink15.com"

    def test_order_book_hash(self):
        c = ClobClient("https://api.blink15.com")
        book = OrderBookSummary(
            market="test", asset_id="abc", timestamp="123",
            bids=[OrderSummary(price="0.50", size="10")],
            asks=[OrderSummary(price="0.60", size="5")],
            min_order_size="1", tick_size="0.01", neg_risk=False,
            last_trade_price="0.55",
        )
        h = c.get_order_book_hash(book)
        assert len(h) == 40  # SHA1 hex
        assert book.hash == h
        # Deterministic
        h2 = c.get_order_book_hash(book)
        assert h == h2
        c.close()
