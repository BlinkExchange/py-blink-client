# /home/shanmu/Documents/crypto/blink/py-blink-client/py_blink_client/constants.py
"""
Blink Markets CLOB client constants.

Contract addresses, endpoints, chain configuration, EIP-712 domains, and defaults.
"""

# ---------------------------------------------------------------------------
# Chain
# ---------------------------------------------------------------------------
BASE_SEPOLIA_CHAIN_ID = 84532

# ---------------------------------------------------------------------------
# V3 contract addresses (Circle USDC, deployed 2026-03-31)
# ---------------------------------------------------------------------------
CONTRACTS = {
    "exchange": "0x6Eb5B3a29A5f20a5Cfed228c96037abeFa0deA2d",
    "usdc": "0x9f702fa37809C1cb4e023f78F801f180a1DF5C8E",
    "ctf": "0x5baC724896651ee24465C7CA517722C4F644A09B",
}

# ---------------------------------------------------------------------------
# EIP-712 domains
# ---------------------------------------------------------------------------
ORDER_DOMAIN_NAME = "Blink Exchange"
ORDER_DOMAIN_VERSION = "1"

AUTH_DOMAIN_NAME = "ClobAuthDomain"
AUTH_DOMAIN_VERSION = "1"

# Message signed during L1 (EIP-712) API-key creation / derivation
AUTH_MSG_TO_SIGN = "This message attests that I control the given wallet"

# ---------------------------------------------------------------------------
# EIP-712 type definitions (order signing)
# ---------------------------------------------------------------------------
ORDER_EIP712_TYPES = {
    "Order": [
        {"name": "salt", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "signer", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "side", "type": "uint8"},
        {"name": "signatureType", "type": "uint8"},
    ],
}

AUTH_EIP712_TYPES = {
    "ClobAuth": [
        {"name": "address", "type": "address"},
        {"name": "timestamp", "type": "string"},
        {"name": "nonce", "type": "uint256"},
        {"name": "message", "type": "string"},
    ],
}

# ---------------------------------------------------------------------------
# Collateral decimals (USDC = 6)
# ---------------------------------------------------------------------------
COLLATERAL_DECIMALS = 6
COLLATERAL_SCALE = 10 ** COLLATERAL_DECIMALS  # 1_000_000

# ---------------------------------------------------------------------------
# Zero address
# ---------------------------------------------------------------------------
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# ---------------------------------------------------------------------------
# Default API base URLs
# ---------------------------------------------------------------------------
DEFAULT_API_URL = "https://api.blink15.com"
DEFAULT_WS_URL = "wss://api.blink15.com"

# ---------------------------------------------------------------------------
# REST endpoints -- Public
# ---------------------------------------------------------------------------
EP_HEALTH = "/health"
EP_HEALTH_READY = "/health/ready"
EP_ADMIN_HEALTH_DETAILED = "/admin/health/detailed"
EP_TIME = "/time"
EP_MARKETS = "/markets"
EP_MARKET = "/markets/"            # + {market_id}
EP_MARKETS_UPCOMING = "/markets/upcoming"
EP_MARKETS_RESOLVING = "/markets/resolving"
EP_BOOK = "/book"
EP_BOOK_UNIFIED = "/book/unified"
EP_BOOKS = "/books"
EP_MIDPOINT = "/midpoint"
EP_MIDPOINTS = "/midpoints"
EP_PRICE = "/price"
EP_PRICES = "/prices"
EP_SPREAD = "/spread"
EP_TICK_SIZE = "/tick-size"
EP_LAST_TRADE_PRICE = "/last-trade-price"
EP_FEE_RATE = "/fee-rate"
EP_NEG_RISK = "/neg-risk"
EP_TICKS = "/ticks/"               # + {symbol}
EP_TICKS_STATS = "/ticks/"         # + {symbol}/stats
EP_PRICE_HISTORY = "/prices/"      # + {token_id}/history
EP_WALLET_STATUS = "/wallet-status"

# ---------------------------------------------------------------------------
# REST endpoints -- Faucet / gas-sponsorship (public, no auth)
# ---------------------------------------------------------------------------
EP_FAUCET_CLAIM = "/faucet/claim"
EP_PERMIT = "/v1/permit"
EP_PREFUND_CTF_APPROVAL = "/v1/prefund-ctf-approval"

# ---------------------------------------------------------------------------
# REST endpoints -- Balance (public, no auth)
# ---------------------------------------------------------------------------
EP_BALANCE_PUBLIC = "/v1/balance"

# ---------------------------------------------------------------------------
# REST endpoints -- Profile (Privy JWT auth)
# ---------------------------------------------------------------------------
EP_PROFILE = "/v1/profile"

# ---------------------------------------------------------------------------
# REST endpoints -- Auth (L1 EIP-712)
# ---------------------------------------------------------------------------
EP_CREATE_API_KEY = "/auth/api-key"
EP_DERIVE_API_KEY = "/auth/derive-api-key"

# ---------------------------------------------------------------------------
# REST endpoints -- Auth (L2 HMAC)
# ---------------------------------------------------------------------------
EP_API_KEYS = "/auth/api-keys"
EP_API_KEY = "/auth/api-key"

# ---------------------------------------------------------------------------
# REST endpoints -- Authenticated (L2 HMAC)
# ---------------------------------------------------------------------------
EP_ORDER = "/order"
EP_ORDERS = "/orders"
EP_CANCEL_ALL = "/cancel-all"
EP_HEARTBEATS = "/v1/heartbeats"
EP_DATA_ORDERS = "/data/orders"
EP_DATA_ORDER = "/data/order/"     # + {order_id}
EP_DATA_TRADES = "/data/trades"
EP_BALANCE_ALLOWANCE = "/balance-allowance"
EP_BALANCE_ALLOWANCE_UPDATE = "/balance-allowance/update"

# ---------------------------------------------------------------------------
# WebSocket paths
# ---------------------------------------------------------------------------
WS_MARKET_PATH = "/ws/market"
WS_PRICE_PATH = "/ws/price"
WS_USER_PATH = "/ws/user"

# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
END_CURSOR = "LTE="
INITIAL_CURSOR = "MA=="
MAX_PAGES = 500

# ---------------------------------------------------------------------------
# HTTP retry defaults
# ---------------------------------------------------------------------------
DEFAULT_RETRIES = 3
BACKOFF_FACTORS = (0.5, 1.0, 2.0)
DEFAULT_TIMEOUT = 30.0
