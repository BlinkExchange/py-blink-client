# py-blink-client

Python SDK for the [Blink](https://blink15.com) prediction market CLOB API. Drop-in replacement for Polymarket's `py-clob-client`.

```
pip install py-blink-client
```

## Quickstart

```python
from py_blink_client import ClobClient, OrderArgs, OrderType

# 1. Create client with your private key
client = ClobClient("https://api.blink15.com", key="0xYOUR_PRIVATE_KEY")

# 2. Get API credentials (creates new key, or derives existing one)
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

# 3. Browse markets (returns paginated dict)
resp = client.get_markets()
market = resp["data"][0]
token_id = market["yes_token_id"]

# 4. Check the orderbook
book = client.get_order_book(token_id)  # returns OrderBookSummary
print(f"Best bid: {book.bids[0].price}, Best ask: {book.asks[0].price}")

# 5. Sign an order locally (does NOT submit)
signed = client.create_order(OrderArgs(
    token_id=token_id,
    side="BUY",
    price=0.50,
    size=20,
))

# 6. Submit the signed order
result = client.post_order(signed, orderType=OrderType.GTC)
print(f"Order placed: {result['order_id']}")

# 7. Cancel it
client.cancel(result["order_id"])
```

## Polymarket Migration

```python
# Before (Polymarket)
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import OrderArgs, OrderType

# After (Blink) -- same API, different import
from py_blink_client import ClobClient, OrderArgs, OrderType
```

All method names, argument shapes, and return types match `py-clob-client`. Blink ignores `neg_risk` (accepted without error for compatibility).

## Installation

```bash
pip install py-blink-client
```

From source:

```bash
git clone https://github.com/blink-markets/py-blink-client
cd py-blink-client
pip install -e .
```

**Requirements:** Python 3.10+, `eth-account`, `requests`, `websockets`

## Authentication

Blink uses two auth layers (same as Polymarket):

**L1 (EIP-712)** -- proves wallet ownership. Used once to create API credentials:

```python
client = ClobClient("https://api.blink15.com", key="0x...")
creds = client.create_or_derive_api_creds()
# creds is an ApiCreds(api_key, api_secret, api_passphrase)
# Also stored on client automatically
```

**L2 (HMAC)** -- used for all trading and private data endpoints. Set automatically after `create_or_derive_api_creds()`, or manually:

```python
from py_blink_client import ApiCreds

creds = ApiCreds(api_key="...", api_secret="...", api_passphrase="...")
client.set_api_creds(creds)

# All subsequent calls use HMAC auth
client.get_orders()
client.post_order(...)
client.cancel(...)
```

## Trading

### Sign + Submit (two-step)

`create_order` signs locally and returns a dict. `post_order` submits it.

```python
from py_blink_client import OrderArgs, OrderType

# Sign locally (no network call)
signed = client.create_order(OrderArgs(
    token_id=token_id,
    side="BUY",
    price=0.55,
    size=100,
))

# Submit to the exchange
resp = client.post_order(signed, orderType=OrderType.GTC)
```

### Sign + Submit (one-step convenience)

```python
resp = client.create_and_post_order(
    OrderArgs(token_id=token_id, side="BUY", price=0.55, size=100),
    orderType=OrderType.GTC,
)
```

### Order Types

| Type | Description |
|------|-------------|
| `OrderType.GTC` | Good-til-cancelled (default) |
| `OrderType.FOK` | Fill-or-kill |
| `OrderType.GTD` | Good-til-date |
| `OrderType.FAK` | Fill-and-kill |

### Post-Only Orders

```python
resp = client.post_order(signed, orderType=OrderType.GTC, post_only=True)
```

### Batch Orders (up to 10)

```python
# One-step: sign + submit all
resp = client.create_and_post_orders([
    OrderArgs(token_id=yes_token, side="BUY", price=0.48, size=50),
    OrderArgs(token_id=yes_token, side="BUY", price=0.47, size=50),
    OrderArgs(token_id=yes_token, side="BUY", price=0.46, size=50),
])
```

### Market Orders

```python
from py_blink_client import MarketOrderArgs

# Walks the orderbook to find fill price, signs as FOK
signed = client.create_market_order(MarketOrderArgs(
    token_id=token_id,
    side="BUY",
    amount=50.0,  # dollar amount for BUY, share count for SELL
))
resp = client.post_order(signed, orderType=OrderType.FOK)

# Or one-step:
resp = client.create_and_post_market_order(
    MarketOrderArgs(token_id=token_id, side="BUY", amount=50.0)
)
```

### Cancel Orders

```python
client.cancel("order-uuid")                     # cancel one
client.cancel_orders(["uuid-1", "uuid-2"])       # cancel multiple
client.cancel_all()                              # cancel all open orders
client.cancel_market_orders(market="market-id")  # cancel all for a market
```

## Market Data

All market data endpoints are public (no auth required):

```python
client = ClobClient("https://api.blink15.com")  # no key needed

# Markets (returns paginated dict with "data", "next_cursor", "count")
resp = client.get_markets()
markets = resp["data"]

# Single market (returns Market dataclass)
market = client.get_market("market-uuid")

# Typed market list (returns List[Market])
typed = client.get_markets_typed()

# Orderbook (returns OrderBookSummary with .bids/.asks of OrderSummary)
book = client.get_order_book(token_id)
for bid in book.bids:
    print(f"  {bid.price} x {bid.size}")

# Prices
mid = client.get_midpoint(token_id)         # {"mid": "0.55"}
spread = client.get_spread(token_id)        # {"spread": "0.02"}
last = client.get_last_trade_price(token_id)

# Batch queries
books = client.get_order_books([token_a, token_b])
mids = client.get_midpoints([token_a, token_b])

# User data (public)
client.get_user_profile("0xAddress")
client.get_user_positions("0xAddress")
client.get_user_trades("0xAddress")
client.get_user_activity("0xAddress")
```

## WebSocket -- Real-Time Data

### Market Orderbook

```python
from py_blink_client import BlinkMarketWs

ws = BlinkMarketWs("https://api.blink15.com")
ws.on_snapshot = lambda data: print(f"Book: {len(data['bids'])} bids")
ws.on_trade = lambda data: print(f"Trade: {data['side']} @ {data['price']}")
ws.subscribe([token_id])   # lowercase hex, no 0x prefix
ws.start()

# Subscribe to all markets
ws.subscribe_all()

# Request a full snapshot for gap recovery
ws.resync(token_id)

# Clean shutdown
ws.stop()
```

Callbacks: `on_snapshot`, `on_best_prices`, `on_trade`, `on_market_created`, `on_market_status`

### Price Ticker (Pyth Oracle)

```python
from py_blink_client import BlinkPriceWs

ws = BlinkPriceWs("https://api.blink15.com")
ws.on_price_tick = lambda data: print(f"{data['asset_id']}: ${data['price']}")
ws.subscribe(["AAPLX", "SPYX", "NVDAX"])  # uses symbols, not token IDs
ws.start()
```

### User Events (authenticated)

```python
from py_blink_client import BlinkUserWs, ApiCreds

creds = ApiCreds(api_key="...", api_secret="...", api_passphrase="...")
ws = BlinkUserWs("https://api.blink15.com", creds)
ws.on_order_fill = lambda fill: print(f"Fill: {fill}")
ws.on_balance_update = lambda data: print(f"Balance: {data}")
ws.on_order_accepted = lambda n: print(f"Accepted: {n}")
ws.start()
ws.wait_for_auth(timeout=10)  # blocks until authenticated
```

Top-level envelope callbacks: `on_authenticated`, `on_auth_error`, `on_ws_error`, `on_pong`, `on_state_changed`, `on_balance_update`, `on_settlement`, `on_wallet_status`, `on_redeemable_position`, `on_activity_created`, `on_notification` (fires before kind dispatch for forward-compat).

Notification-kind callbacks (called with the inner `data` dict): `on_order_accepted`, `on_order_rejected`, `on_order_fill`, `on_order_cancelled`, `on_redemption`.

## Testnet Utilities

```python
# Mint 100 USDC (Base Sepolia testnet only)
client.claim_faucet("0xYourAddress")

# Gas-free USDC approval via EIP-2612 permit
client.relay_permit(owner, spender, value, deadline, v, r, s)

# Sponsor ETH for CTF token approval
client.prefund_ctf_approval("0xYourAddress")
```

## API Reference

### Market Data (no auth)

| Method | Returns | Description |
|--------|---------|-------------|
| `get_markets(next_cursor=)` | `dict` | Paginated markets (`{"data": [...], "next_cursor": ...}`) |
| `get_markets_typed()` | `List[Market]` | All markets as typed dataclasses |
| `get_market(market_id)` | `Market` | Single market by UUID |
| `get_markets_upcoming(limit=10)` | `Any` | Markets closing soonest |
| `get_markets_resolving()` | `Any` | Markets past close time |
| `get_order_book(token_id)` | `OrderBookSummary` | Orderbook with `.bids`/`.asks` |
| `get_book_unified(token_id)` | `dict` | Orderbook with implied liquidity |
| `get_order_books(token_ids)` | `Any` | Batch orderbooks |
| `get_midpoint(token_id)` | `dict` | Mid price |
| `get_midpoints(token_ids)` | `Any` | Batch midpoints |
| `get_price(token_id, side=)` | `dict` | Best bid or ask |
| `get_prices(requests)` | `Any` | Batch prices |
| `get_spread(token_id)` | `dict` | Bid-ask spread |
| `get_last_trade_price(token_id)` | `dict` | Last fill price |
| `get_tick_size(token_id)` | `dict` | Min tick + order size |
| `get_fee_rate(token_id)` | `dict` | Fee structure |
| `get_price_ticks(symbol)` | `Any` | 2-min price history |
| `get_wallet_status(address)` | `dict` | USDC + ETH balances |
| `health()` | `Any` | Service status |
| `get_server_time()` | `Any` | Server timestamp |

### User Data (public, no auth)

| Method | Description |
|--------|-------------|
| `get_user_profile(address)` | Profile stats |
| `get_user_activity(address, limit=50, offset=0)` | Activity feed |
| `get_user_orders(address)` | Open orders |
| `get_user_trades(address)` | Trade history |
| `get_user_positions(address)` | Token balances |
| `get_user_redemptions(address)` | Redemption history |
| `get_user_history(address)` | On-chain events |
| `get_portfolio_history(address)` | Portfolio chart data |

### Trading (HMAC auth required)

| Method | Returns | Description |
|--------|---------|-------------|
| `create_order(args)` | `dict` | Sign order locally (no network call) |
| `post_order(order, orderType=, post_only=)` | `dict` | Submit signed order |
| `create_and_post_order(args, orderType=)` | `dict` | Sign + submit in one call |
| `create_market_order(args)` | `dict` | Sign market order locally (walks book) |
| `create_and_post_market_order(args)` | `dict` | Sign + submit market order |
| `post_orders(orders)` | `Any` | Submit batch of signed orders (max 10) |
| `create_and_post_orders(args_list)` | `Any` | Sign + submit batch (max 10) |
| `cancel(order_id)` | `dict` | Cancel one order |
| `cancel_orders(order_ids)` | `dict` | Cancel multiple orders |
| `cancel_all()` | `dict` | Cancel all open orders |
| `cancel_market_orders(market=, asset_id=)` | `dict` | Cancel orders for a market/asset |
| `get_orders(params=)` | `List[dict]` | Open orders (L2) |
| `get_order(order_id)` | `OpenOrder` | Single order by ID |
| `get_trades(params=)` | `List[dict]` | Trade history (L2) |
| `get_balance_allowance(asset_type=)` | `BalanceAllowance` | USDC/CTF balance |
| `update_balance_allowance(asset_type=)` | `Any` | Force-refresh cached balance |
| `list_orders(market_id=, maker=, status=, limit=)` | `Any` | Filter orders via /orders |

### Auth (L1 EIP-712)

| Method | Returns | Description |
|--------|---------|-------------|
| `create_api_key()` | `ApiCreds` | Create new credentials |
| `derive_api_key()` | `ApiCreds` | Recover existing credentials |
| `create_or_derive_api_creds()` | `ApiCreds` | Create, fallback to derive |
| `set_api_creds(creds)` | `None` | Set credentials manually |
| `get_api_keys()` | `Any` | List all API keys |
| `delete_api_key()` | `Any` | Revoke current key |

### Testnet

| Method | Description |
|--------|-------------|
| `claim_faucet(address)` | Mint 100 USDC (testnet) |
| `relay_permit(owner, spender, value, deadline, v, r, s)` | Gas-free USDC approval |
| `prefund_ctf_approval(address)` | Sponsor ETH for CTF approval |
| `update_profile(did, username, bio=)` | Set display name |

## Backward-Compatibility Aliases

These old names still work:

| Alias | Points to |
|-------|-----------|
| `BlinkClobClient` | `ClobClient` |
| `get_book(token_id)` | `get_order_book(token_id)` |
| `get_books_batch(ids)` | `get_order_books(ids)` |
| `get_midpoints_batch(ids)` | `get_midpoints(ids)` |
| `get_prices_batch(reqs)` | `get_prices(reqs)` |
| `cancel_order(id)` | `cancel(id)` |
| `create_orders(args_list)` | `create_and_post_orders(args_list)` |
| `create_or_derive_api_key()` | `create_or_derive_api_creds()` |
| `list_api_keys()` | `get_api_keys()` |

## Contract Addresses (Base Sepolia)

| Contract | Address |
|----------|---------|
| Exchange (UUPS proxy) | `0x6Eb5B3a29A5f20a5Cfed228c96037abeFa0deA2d` |
| USDC (Circle FiatTokenV2_2) | `0x9f702fa37809C1cb4e023f78F801f180a1DF5C8E` |
| CTF | `0x5baC724896651ee24465C7CA517722C4F644A09B` |
| Chain ID | 84532 |

Available in code via:

```python
from py_blink_client import CONTRACTS, BASE_SEPOLIA_CHAIN_ID

print(CONTRACTS["exchange"])  # 0x6Eb5B3a29A5f20a5Cfed228c96037abeFa0deA2d
print(CONTRACTS["usdc"])      # 0x9f702fa37809C1cb4e023f78F801f180a1DF5C8E
print(CONTRACTS["ctf"])       # 0x5baC724896651ee24465C7CA517722C4F644A09B
```

## License

MIT
