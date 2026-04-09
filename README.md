# py-blink-client

Python client for the [Blink Markets](https://blink15.com) CLOB.

Sync and async variants, EIP-712 order signing, HMAC L2 auth, WebSocket
market/price/user feeds.

## Install

```bash
pip install py-blink-client
```

From source:

```bash
git clone https://github.com/BlinkExchange/py-blink-client
cd py-blink-client
pip install -e .
```

## Quick start

```python
from py_blink_client import ClobClient, OrderArgs, OrderType

client = ClobClient("https://api.blink15.com", key="0x...")
creds = client.create_or_derive_api_creds()
client.set_api_creds(creds)

markets = client.get_markets()
token = markets["data"][0]["yes_token_id"]

order = client.create_order(OrderArgs(token_id=token, price=0.55, size=10, side="BUY"))
resp = client.post_order(order, orderType=OrderType.GTC)
print(resp)
```

Async:

```python
import asyncio
from py_blink_client import AsyncClobClient, OrderArgs

async def main():
    client = AsyncClobClient("https://api.blink15.com", key="0x...")
    await client.create_and_post_order(
        OrderArgs(token_id="...", price=0.55, size=10, side="BUY")
    )
    await client.close()

asyncio.run(main())
```

## WebSockets

```python
from py_blink_client import BlinkMarketWs

ws = BlinkMarketWs("wss://api.blink15.com")
ws.on_snapshot = lambda d: print("book", d)
ws.subscribe(["<token_id_hex_no_0x>"])
ws.start()
```

## Examples

- `examples/glft_market_maker.py` — GLFT-based market maker for SPY binary
  markets. Runs on its own wallet; set `MM_PRIVATE_KEY` or let it generate
  a fresh one and pull faucet funds.
- `examples/verify_backend.py` — smoke-test every SDK method against a live
  backend.

## Tests

```bash
pip install -e '.[dev]'
pytest
```

## License

MIT
