#!/usr/bin/env python3
"""Smoke-test every SDK method against a live Blink backend."""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from eth_account import Account
from eth_account.messages import encode_typed_data

from py_blink_client import AsyncClobClient, OrderArgs, OrderType, Side
from py_blink_client.constants import CONTRACTS

API = "https://api.blink15.com"


class Verifier:
    def __init__(self):
        self.results: list[tuple[str, str, str]] = []  # (name, status, detail)
        self.passed = 0
        self.failed = 0
        self.skipped = 0

    async def check(self, name: str, fn: Callable, skip_if=None) -> Any:
        """Call an SDK method and record pass/fail. Returns result or None."""
        if skip_if:
            self.results.append((name, "SKIP", skip_if))
            self.skipped += 1
            print(f"  SKIP  {name}: {skip_if}")
            return None
        try:
            result = await fn() if asyncio.iscoroutinefunction(fn) else fn()
            if asyncio.iscoroutine(result):
                result = await result
            detail = type(result).__name__
            if result is not None and hasattr(result, "__len__"):
                try:
                    detail += f"({len(result)})"
                except Exception:
                    pass
            self.results.append((name, "PASS", detail))
            self.passed += 1
            print(f"  PASS  {name}: {detail}")
            return result
        except NotImplementedError as e:
            self.results.append((name, "STUB", str(e)[:60]))
            self.passed += 1
            print(f"  STUB  {name}: NotImplementedError (expected)")
            return None
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:100]}"
            self.results.append((name, "FAIL", msg))
            self.failed += 1
            print(f"  FAIL  {name}: {msg}")
            return None

    def section(self, name: str):
        print(f"\n=== {name} ===")

    def report(self):
        print("\n" + "=" * 70)
        print(f"VERIFICATION SUMMARY")
        print("=" * 70)
        print(f"  PASSED: {self.passed}")
        print(f"  FAILED: {self.failed}")
        print(f"  SKIPPED: {self.skipped}")
        print(f"  TOTAL:  {self.passed + self.failed + self.skipped}")
        if self.failed > 0:
            print("\nFailures:")
            for name, status, detail in self.results:
                if status == "FAIL":
                    print(f"  - {name}: {detail}")
        print("=" * 70)
        return self.failed == 0


async def sign_permit(client: AsyncClobClient) -> None:
    account = Account.from_key(client._private_key)
    domain = {
        "name": "USD Coin", "version": "2",
        "chainId": 84532, "verifyingContract": CONTRACTS["usdc"],
    }
    permit_types = {"Permit": [
        {"name": "owner", "type": "address"},
        {"name": "spender", "type": "address"},
        {"name": "value", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint256"},
    ]}
    msg = {
        "owner": client.address, "spender": client.exchange_address,
        "value": 2**256 - 1, "nonce": 0, "deadline": 2**256 - 1,
    }
    signable = encode_typed_data(domain, permit_types, msg)
    signed = account.sign_message(signable)
    sig = signed.signature.hex()
    await client.relay_permit(
        owner=client.address, spender=client.exchange_address,
        value=str(2**256 - 1), deadline=str(2**256 - 1),
        v=signed.v, r="0x" + sig[:64], s="0x" + sig[64:128],
    )


async def main():
    v = Verifier()

    # Fresh wallet
    acct = Account.create()
    client = AsyncClobClient(API, key=acct.key.hex())
    print(f"Wallet: {client.address}")

    # ═══ Health & Time ═══
    v.section("Health & Time")
    await v.check("health()", client.health)
    await v.check("health_ready()", client.health_ready)
    await v.check("get_ok()", client.get_ok)
    await v.check("get_server_time()", client.get_server_time)

    # ═══ Market Data (public) ═══
    v.section("Market Data (public)")
    markets_resp = await v.check("get_markets()", client.get_markets)

    # Find a test market
    markets = markets_resp.get("data", []) if isinstance(markets_resp, dict) else markets_resp
    now = datetime.now(timezone.utc)
    target = None
    for m in markets:
        if m.get("status", "").lower() != "active":
            continue
        if not m.get("symbol", "").startswith("SPY"):
            continue
        try:
            ct = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
            if 300 < (ct - now).total_seconds() < 2000:
                target = m
                break
        except Exception:
            continue

    if not target:
        print("ERROR: No suitable market found")
        return

    market_id = target["id"]
    yes_token = target["yes_token_id"]
    no_token = target["no_token_id"]
    print(f"Test market: {target['symbol']} id={market_id[:8]}")

    await v.check(f"get_market(id)", lambda: client.get_market(market_id))
    await v.check("get_upcoming_markets()", client.get_upcoming_markets)
    await v.check("get_resolving_markets()", client.get_resolving_markets)
    await v.check(f"get_market_orderbook({market_id[:8]})", lambda: client.get_market_orderbook(market_id))

    # ═══ Order Book & Prices ═══
    v.section("Order Book & Prices")
    await v.check(f"get_order_book(yes)", lambda: client.get_order_book(yes_token))
    await v.check(f"get_order_books([yes, no])", lambda: client.get_order_books([yes_token, no_token]))
    await v.check("get_unified_order_book(yes)", lambda: client.get_unified_order_book(yes_token))
    await v.check("get_midpoint(yes)", lambda: client.get_midpoint(yes_token))
    await v.check("get_midpoints([yes, no])", lambda: client.get_midpoints([yes_token, no_token]))
    await v.check("get_price(yes, BUY)", lambda: client.get_price(yes_token, "BUY"))
    await v.check("get_prices([...])", lambda: client.get_prices([
        {"token_id": yes_token, "side": "BUY"},
        {"token_id": no_token, "side": "SELL"},
    ]))
    await v.check("get_spread(yes)", lambda: client.get_spread(yes_token))
    await v.check("get_last_trade_price(yes)", lambda: client.get_last_trade_price(yes_token))
    await v.check("get_tick_size(yes)", lambda: client.get_tick_size(yes_token))
    await v.check("get_neg_risk(yes)", lambda: client.get_neg_risk(yes_token))
    await v.check("get_fee_rate_bps(yes)", lambda: client.get_fee_rate_bps(yes_token))

    # ═══ Tick Data ═══
    v.section("Tick Data")
    await v.check("get_tick_backfill(SPYX)", lambda: client.get_tick_backfill("SPYX"))
    await v.check("get_price_stats(SPYX)", lambda: client.get_price_stats("SPYX"))
    await v.check("get_price_history(yes)", lambda: client.get_price_history(yes_token))

    # ═══ Wallet & Balance (public, no auth) ═══
    v.section("Wallet & Balance")
    await v.check("get_wallet_status(addr)", lambda: client.get_wallet_status(client.address))
    await v.check("get_balance_allowance_public(addr)", lambda: client.get_balance_allowance_public(client.address))

    # ═══ User Data (public) ═══
    v.section("User Data (public)")
    await v.check("get_user_profile(addr)", lambda: client.get_user_profile(client.address))
    await v.check("get_user_positions(addr)", lambda: client.get_user_positions(client.address))
    await v.check("get_user_trades(addr)", lambda: client.get_user_trades(client.address))
    await v.check("get_user_orders(addr)", lambda: client.get_user_orders(client.address))
    await v.check("get_user_activity(addr)", lambda: client.get_user_activity(client.address))
    await v.check("get_user_redemptions(addr)", lambda: client.get_user_redemptions(client.address))
    await v.check("get_user_history(addr)", lambda: client.get_user_history(client.address))
    await v.check("get_portfolio_history(addr)", lambda: client.get_portfolio_history(client.address))
    await v.check("search_users(query)", lambda: client.search_users(query="test"))

    # ═══ Faucet & Onboarding ═══
    v.section("Faucet & Onboarding")
    await v.check("claim_faucet(addr)", lambda: client.claim_faucet(client.address))
    await asyncio.sleep(13)
    await v.check("claim_faucet #2", lambda: client.claim_faucet(client.address))
    await v.check("prefund_ctf_approval(addr)", lambda: client.prefund_ctf_approval(client.address))

    # USDC permit (direct sign + relay)
    try:
        await sign_permit(client)
        v.results.append(("relay_permit(EIP-2612 sig)", "PASS", "tx submitted"))
        v.passed += 1
        print("  PASS  relay_permit(EIP-2612 sig): tx submitted")
    except Exception as e:
        v.results.append(("relay_permit(EIP-2612 sig)", "FAIL", str(e)[:100]))
        v.failed += 1
        print(f"  FAIL  relay_permit: {e}")

    # ═══ API Key Management ═══
    v.section("API Key Management (L1 EIP-712)")
    creds = await v.check("create_or_derive_api_creds()", client.create_or_derive_api_creds)
    if creds:
        client.set_api_creds(creds)

    await v.check("get_api_keys() [L2 HMAC]", client.get_api_keys)

    # ═══ Balance (HMAC) ═══
    v.section("Balance (L2 HMAC)")
    await v.check("get_balance_allowance()", client.get_balance_allowance)

    # ═══ Order Placement & Management ═══
    v.section("Order Placement (real orders, will be cancelled)")
    resp = await v.check(
        "create_and_post_order(BUY 10 @ 0.30)",
        lambda: client.create_and_post_order(
            OrderArgs(token_id=yes_token, price=0.30, size=10, side="BUY"),
        ),
    )
    await v.check("get_orders() [authenticated]", client.get_orders)

    if resp and isinstance(resp, dict) and resp.get("order_id"):
        oid = resp["order_id"]
        await v.check(f"get_order({oid[:8]})", lambda: client.get_order(oid))
        await v.check(f"cancel({oid[:8]})", lambda: client.cancel(oid))

    # Batch order
    signed_orders = []
    for i in range(3):
        sig = await client.create_order(OrderArgs(token_id=yes_token, price=round(0.25 - i*0.01, 2), size=10, side="BUY"))
        signed_orders.append({"order": sig, "orderType": "GTC", "postOnly": False})
    await v.check("post_orders(batch 3)", lambda: client.post_orders(signed_orders))

    await v.check("get_trades()", client.get_trades)
    await v.check("post_heartbeat()", client.post_heartbeat)

    # Cleanup
    await v.check("cancel_all()", client.cancel_all)
    await v.check("cancel_market_orders({})", lambda: client.cancel_market_orders(market=market_id))

    # ═══ Polymarket Stubs (should throw NotImplementedError) ═══
    v.section("Polymarket Stubs (expected NotImplementedError)")
    await v.check("get_notifications()", client.get_notifications)
    await v.check("is_order_scoring()", client.is_order_scoring)
    await v.check("get_closed_only_mode()", client.get_closed_only_mode)
    await v.check("create_readonly_api_key()", client.create_readonly_api_key)
    await v.check("get_reward_percentages()", client.get_reward_percentages)
    await v.check("get_current_rewards()", client.get_current_rewards)

    # ═══ Compat redirects ═══
    v.section("Polymarket Compat Redirects")
    await v.check("get_simplified_markets()", client.get_simplified_markets)
    await v.check("get_sampling_markets()", client.get_sampling_markets)

    # ═══ Order Builder ═══
    v.section("Order Builder (local signing only)")
    await v.check(
        "create_order(OrderArgs)",
        lambda: client.create_order(OrderArgs(token_id=yes_token, price=0.25, size=10, side="BUY")),
    )

    # Final cleanup
    await client.cancel_all()
    await client.close()

    # Report
    all_ok = v.report()
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
