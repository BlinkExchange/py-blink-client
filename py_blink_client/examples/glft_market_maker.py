#!/usr/bin/env python3
"""
Production Market Maker -- Shaw & Dalen (2025) Framework

Implements the Avellaneda-Stoikov market making model adapted for binary
prediction markets using the logit-space framework from:

  Shaw & Dalen, "Toward Black-Scholes for Prediction Markets:
  A Unified Kernel and Market-Maker's Handbook", arXiv:2510.15205, 2025

All pricing parameters derive from three business inputs:
  - CAPITAL: total USDC available ($2,000)
  - MAX_LOSS: maximum acceptable directional loss ($100)
  - TICK_SIZE: exchange minimum price increment ($0.01)

Everything else -- fair value, belief volatility, risk aversion, spread,
reservation price, inventory skew -- is computed from the Pyth price feed
and these three inputs using the exact formulas from the paper.

Usage:
  cd py-blink-client
  set -a && source ../backend/.env && set +a
  python3 -m py_blink_client.examples.glft_market_maker
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set

# Ensure the package is importable when running as a script
if __name__ == "__main__":
    _pkg_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if _pkg_dir not in sys.path:
        sys.path.insert(0, _pkg_dir)

from py_blink_client import (
    AsyncClobClient,
    ApiCreds,
    BlinkMarketWs,
    BlinkPriceWs,
    BlinkUserWs,
    OrderArgs,
    OrderType,
)
from py_blink_client.glft_solver import GLFTSolution, solve_glft

# ============================================================================
# Logging
# ============================================================================

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("glft_mm")
logger.setLevel(logging.DEBUG)

# ============================================================================
# Business Inputs (the ONLY human-chosen parameters)
# ============================================================================

CAPITAL = float(os.environ.get("MM_CAPITAL", "2000"))       # USDC
MAX_LOSS = float(os.environ.get("MM_MAX_LOSS", "100"))      # max directional loss
TICK_SIZE = 0.01                                              # exchange tick size

# Infrastructure config (not pricing)
BACKEND_URL = os.environ.get("BACKEND_URL", "https://api.blink15.com")
WS_URL = os.environ.get("WS_URL", "wss://api.blink15.com")
SYMBOL = os.environ.get("MM_SYMBOL", "SPYX")
LEVELS = 5

# Requote: event-driven, fires on every meaningful price change.
REQUOTE_THRESHOLD_LOGIT = 0.03  # ~0.75c at p=0.50

# Feed staleness: cancel all orders if no Pyth tick for this long
STALE_FEED_MS = 10_000  # 10 seconds

# Adverse selection: widen spread if fills are too one-sided
TOXICITY_WINDOW = 20
TOXICITY_THRESHOLD = 0.80
TOXICITY_MULTIPLIER = 2.0


# ============================================================================
# Math Primitives
# ============================================================================

def normal_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def logit(p: float) -> float:
    p = max(1e-8, min(1.0 - 1e-8, p))
    return math.log(p / (1.0 - p))


def sigmoid(x: float) -> float:
    if x > 500:
        return 1.0
    if x < -500:
        return 0.0
    return 1.0 / (1.0 + math.exp(-x))


# ============================================================================
# Shaw & Dalen (2025) -- Core Pricing Engine
# ============================================================================

# SPY annualized volatility (~16%). Used to compute per-second vol.
SIGMA_S_ANNUAL = float(os.environ.get("MM_SIGMA_ANNUAL", "0.16"))
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # ~5.9M seconds
SIGMA_S_PER_SEC = SIGMA_S_ANNUAL / math.sqrt(TRADING_SECONDS_PER_YEAR)

# Expected order arrival rate (orders/second at mid-price).
A_ARRIVAL = float(os.environ.get("MM_ARRIVAL_RATE", "0.01"))

# Active GLFT solver -- recomputed when sigma_b or parameters change significantly
_active_solver: Optional[GLFTSolution] = None
_solver_sigma_b: float = 0.0
_solver_Q: int = 0
_solver_gamma: float = 0.0
_solver_k: float = 0.0


@dataclass
class QuoteResult:
    fair_value: float
    yes_bid: float
    yes_ask: float
    sigma_b: float
    gamma: float
    k: float
    Q_MAX: int
    c1: float
    c2: float  # no longer used -- exact ODE replaces asymptotic c2
    half_spread: float
    r_x: float  # reservation price in logit space


def compute_quotes(S: float, K: float, tau_secs: float, q: int) -> QuoteResult:
    """Compute all derived pricing parameters from market state.

    Uses the exact GLFT finite-horizon ODE solution (Theorem 1).
    sigma_b derived from Ito's lemma on binary option pricing (Shaw & Dalen 2025).
    """
    global _active_solver, _solver_sigma_b, _solver_Q, _solver_gamma, _solver_k

    # Step 1: Binary option fair value via Black-Scholes d2
    tau = max(1.0, tau_secs)
    sqrt_tau = math.sqrt(tau)
    d2 = (math.log(S / K) - (SIGMA_S_PER_SEC ** 2 / 2.0) * tau) / (SIGMA_S_PER_SEC * sqrt_tau)
    p = max(0.02, min(0.98, normal_cdf(d2)))

    # Step 2: Belief volatility sigma_b (Ito's lemma: binary option -> logit space)
    n_d2 = normal_pdf(d2)
    pq = max(1e-6, p * (1.0 - p))
    sigma_b = n_d2 / (pq * sqrt_tau)

    # Step 3: Hard inventory limit -- bounds actual risk to MAX_LOSS
    Q_MAX = max(10, int(math.floor(MAX_LOSS / max(p, 1.0 - p))))

    # Step 4: Risk aversion gamma (derived from max loss and bounded inventory)
    sigma_b_tau = sigma_b * sigma_b * tau
    gamma = MAX_LOSS / (max(1, Q_MAX) ** 2 * max(0.001, sigma_b_tau))

    # Step 5: Order arrival decay k (from tick size, numerically stable)
    tick_logit = abs(
        logit(min(0.99, p + TICK_SIZE / 2.0))
        - logit(max(0.01, p - TICK_SIZE / 2.0))
    )
    k = gamma / math.expm1(gamma * tick_logit)

    # Step 6: Recompute GLFT ODE solver if parameters changed significantly
    alpha = (k / 2.0) * gamma * sigma_b * sigma_b
    eta = A_ARRIVAL * math.pow(1.0 + gamma / k, -(1.0 + k / gamma))
    inner = max(1e-10, alpha / max(1e-10, eta))
    sigma_q = 1.0 / math.sqrt(math.sqrt(inner))
    Q_effective = max(10, min(Q_MAX, math.ceil(8 * sigma_q)))

    sigma_changed = abs(sigma_b - _solver_sigma_b) / max(0.001, _solver_sigma_b) > 0.2
    q_changed = abs(Q_effective - _solver_Q) / max(1, _solver_Q) > 0.15
    gamma_changed = abs(gamma - _solver_gamma) / max(1e-8, _solver_gamma) > 0.2

    if _active_solver is None or sigma_changed or q_changed or gamma_changed:
        _active_solver = solve_glft(
            gamma=gamma, k=k, sigma_b=sigma_b, A=A_ARRIVAL, Q=Q_effective,
        )
        _solver_sigma_b = sigma_b
        _solver_Q = Q_effective
        _solver_gamma = gamma
        _solver_k = k
        log(f"  GLFT: N={_active_solver.N} {_active_solver.eigen_time*1000:.1f}ms "
            f"sigma_q={sigma_q:.1f} Q_eff={Q_effective} Q_max={Q_MAX}")

    # Step 7: Exact bid/ask depths from ODE solution (GLFT Theorem 1)
    x = logit(p)
    bid_depth_logit = _active_solver.bid_depth(q, tau)
    ask_depth_logit = _active_solver.ask_depth(q, tau)

    # Step 8: Convert to probability space via sigmoid
    p_bid = sigmoid(x - bid_depth_logit) if math.isfinite(bid_depth_logit) else 0.01
    p_ask = sigmoid(x + ask_depth_logit) if math.isfinite(ask_depth_logit) else 0.99

    return QuoteResult(
        fair_value=p,
        yes_bid=math.floor(p_bid * 100) / 100.0,
        yes_ask=math.ceil(p_ask * 100) / 100.0,
        sigma_b=sigma_b,
        gamma=gamma,
        k=k,
        Q_MAX=Q_MAX,
        c1=_active_solver.c1,
        c2=0.0,
        half_spread=(bid_depth_logit + ask_depth_logit) / 2.0,
        r_x=x - q * gamma * sigma_b_tau,
    )


# ============================================================================
# Types
# ============================================================================

@dataclass
class MarketInfo:
    id: str
    symbol: str
    yes_token_id: str
    no_token_id: str
    close_time: str
    open_time: str
    open_price: float


@dataclass
class OrderSpec:
    token_id: str
    side: str  # "BUY" or "SELL"
    price: float
    size: int


@dataclass
class InventoryState:
    yes_tokens: float = 0.0
    no_tokens: float = 0.0


@dataclass
class TrackedOrder:
    id: str
    token_id: str  # normalized hex
    side: str       # "BUY" or "SELL"
    price: float
    size: float


# ============================================================================
# Helpers
# ============================================================================

def log(msg: str) -> None:
    ts = time.strftime("%H:%M:%S", time.localtime())
    print(f"[MM {ts}] {msg}", flush=True)


def ensure_0x(hex_str: str) -> str:
    return hex_str if hex_str.startswith("0x") else f"0x{hex_str}"


def normalize_token_id(id_str: str) -> Optional[str]:
    """Normalize a token ID to lowercase 64-char hex (no 0x prefix)."""
    s = id_str.strip()
    if not s:
        return None
    if s.startswith("0x") or s.startswith("0X"):
        hex_str = s[2:].lower()
    else:
        try:
            hex_str = format(int(s), "064x")
        except (ValueError, OverflowError):
            return None
    if len(hex_str) != 64:
        return None
    import re
    if not re.match(r"^[0-9a-f]{64}$", hex_str):
        return None
    return hex_str


PRICE_TOLERANCE = 0.001


# ============================================================================
# Grid Builder
# ============================================================================

def build_grid(
    yes_token_id: str,
    no_token_id: str,
    quotes: QuoteResult,
    inventory: InventoryState,
) -> List[OrderSpec]:
    orders: List[OrderSpec] = []
    net = inventory.yes_tokens - inventory.no_tokens

    # Tokens per order: distribute capital across levels, both sides
    avg_price = max(0.10, quotes.fair_value)
    tokens_per_order = max(10, min(50,
        int(math.floor(CAPITAL / (LEVELS * 4 * avg_price)))
    ))

    # Remaining capacity before hitting hard inventory limit
    yes_capacity = max(0, int(quotes.Q_MAX - net))    # how many more YES we can buy
    no_capacity = max(0, int(quotes.Q_MAX + net))      # how many more NO we can buy

    yes_placed = 0
    no_placed = 0

    for level in range(LEVELS):
        yes_bid = quotes.yes_bid - level * TICK_SIZE
        no_bid = (1.0 - quotes.yes_ask) - level * TICK_SIZE

        # YES side: hard stop at inventory limit
        if 0.02 <= yes_bid <= 0.98 and yes_placed < yes_capacity:
            min_size = math.ceil(1.0 / yes_bid) + 1
            size = min(
                max(min_size, tokens_per_order),
                yes_capacity - yes_placed,
            )
            if size >= min_size and yes_bid + max(0, no_bid) < 0.99:
                orders.append(OrderSpec(
                    token_id=yes_token_id, side="BUY",
                    price=yes_bid, size=size,
                ))
                yes_placed += size

        # NO side: hard stop at inventory limit
        if 0.02 <= no_bid <= 0.98 and no_placed < no_capacity:
            min_size = math.ceil(1.0 / no_bid) + 1
            size = min(
                max(min_size, tokens_per_order),
                no_capacity - no_placed,
            )
            if size >= min_size and max(0, yes_bid) + no_bid < 0.99:
                orders.append(OrderSpec(
                    token_id=no_token_id, side="BUY",
                    price=no_bid, size=size,
                ))
                no_placed += size

    return orders


# ============================================================================
# Market Discovery
# ============================================================================

async def fetch_latest_market(client: AsyncClobClient, symbol: str) -> Optional[MarketInfo]:
    data = await client.get_markets()
    markets_list = data.get("data", [])
    now_ms = time.time() * 1000.0
    best: Optional[MarketInfo] = None

    for m in markets_list:
        if (m.get("symbol") or m.get("question")) != symbol:
            continue
        if (m.get("status") or "").lower() != "active":
            continue

        open_time_str = m.get("open_time", "")
        close_time_str = m.get("close_time", "")
        try:
            from datetime import datetime, timezone
            open_ms = datetime.fromisoformat(open_time_str.replace("Z", "+00:00")).timestamp() * 1000
            close_ms = datetime.fromisoformat(close_time_str.replace("Z", "+00:00")).timestamp() * 1000
        except (ValueError, AttributeError):
            continue

        if now_ms < open_ms:
            continue
        if close_ms <= now_ms:
            continue

        open_price_raw = int(m.get("open_price", "0"))
        if open_price_raw <= 0:
            continue

        candidate = MarketInfo(
            id=m["id"],
            symbol=m.get("symbol") or m.get("question", ""),
            yes_token_id=ensure_0x(m.get("yes_token_id", "")),
            no_token_id=ensure_0x(m.get("no_token_id", "")),
            close_time=close_time_str,
            open_time=open_time_str,
            open_price=open_price_raw / 1e8,
        )

        if best is None or close_time_str < best.close_time:
            best = candidate

    return best


# ============================================================================
# Main Market Maker
# ============================================================================

class MarketMaker:
    """Production GLFT market maker using the Python SDK."""

    def __init__(self) -> None:
        # State
        self.active_market: Optional[MarketInfo] = None
        self.current_price: float = 0.0
        self.last_reservation: float = -999.0
        self.requoting: bool = False
        self.last_pyth_tick_ms: float = time.time() * 1000.0
        self.orders_live: bool = False
        self.inventory = InventoryState()
        self.provisioned_markets: Set[str] = set()

        # Local order state (replaces REST polling)
        self.local_orders: Dict[str, TrackedOrder] = {}

        # Adverse selection tracking
        self.recent_fill_sides: List[str] = []
        self.toxicity_active: bool = False

        # Edge accounting
        self.total_edge: float = 0.0
        self.total_fills: int = 0

        # Shutdown flag
        self.shutting_down: bool = False

        # Client and WS handles -- set in run()
        self.client: Optional[AsyncClobClient] = None
        self.creds: Optional[ApiCreds] = None
        self.market_ws: Optional[BlinkMarketWs] = None
        self.price_ws: Optional[BlinkPriceWs] = None
        self.user_ws: Optional[BlinkUserWs] = None

        # asyncio loop reference for scheduling coroutines from sync callbacks
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # ------------------------------------------------------------------
    # Toxicity tracking
    # ------------------------------------------------------------------

    def record_fill_side(self, side: str) -> None:
        self.recent_fill_sides.append(side)
        if len(self.recent_fill_sides) > TOXICITY_WINDOW:
            self.recent_fill_sides.pop(0)

        if len(self.recent_fill_sides) >= TOXICITY_WINDOW:
            yes_count = sum(1 for s in self.recent_fill_sides if s == "yes")
            ratio = yes_count / len(self.recent_fill_sides)
            was_active = self.toxicity_active
            self.toxicity_active = (
                ratio > TOXICITY_THRESHOLD
                or ratio < (1.0 - TOXICITY_THRESHOLD)
            )
            if self.toxicity_active and not was_active:
                log(f"  TOXICITY: {ratio * 100:.0f}% one-sided fills -- widening spread")
            elif not self.toxicity_active and was_active:
                log("  Toxicity cleared -- normal spread")

    # ------------------------------------------------------------------
    # Edge accounting
    # ------------------------------------------------------------------

    def record_edge(self, fill_price: float, fair_value: float, side: str, size: float) -> None:
        if side == "buy":
            edge = (fair_value - fill_price) * size
        else:
            edge = (fill_price - fair_value) * size
        self.total_edge += edge
        self.total_fills += 1
        if self.total_fills % 10 == 0:
            avg = self.total_edge / self.total_fills
            log(f"  Edge: ${self.total_edge:.2f} over {self.total_fills} fills (avg ${avg:.3f}/fill)")

    # ------------------------------------------------------------------
    # Local order state
    # ------------------------------------------------------------------

    async def seed_local_orders(self) -> None:
        """Seed local state from REST on startup. Called once."""
        assert self.client is not None
        try:
            orders = await self.client.get_orders()
            arr = orders if isinstance(orders, list) else []
            for o in arr:
                tid = normalize_token_id(o.get("asset_id", ""))
                if not tid:
                    continue
                size_str = o.get("size_remaining") or o.get("original_size") or "0"
                self.local_orders[o["id"]] = TrackedOrder(
                    id=o["id"],
                    token_id=tid,
                    side=o.get("side", "").upper(),
                    price=float(o.get("price", 0)),
                    size=float(size_str) / 1e6,
                )
            log(f"  Seeded {len(self.local_orders)} orders from REST")
        except Exception:
            log("  Failed to seed orders from REST -- starting with empty state")

    async def converge_orders(self, desired_grid: List[OrderSpec]) -> Dict[str, int]:
        """Converge local orders toward the desired grid. Zero REST calls for reads.

        Reads from local_orders (updated by WS events + HTTP responses).
        Computes diff: cancel stale, place new. Book is never fully empty.
        """
        assert self.client is not None
        existing = list(self.local_orders.values())

        # Match existing to desired
        matched_existing: Set[str] = set()
        matched_desired: Set[int] = set()

        for ex in existing:
            for i, d in enumerate(desired_grid):
                if i in matched_desired:
                    continue
                d_token = normalize_token_id(d.token_id)
                if not d_token:
                    continue
                d_side = d.side.upper()
                if (ex.token_id == d_token
                        and ex.side == d_side
                        and abs(ex.price - d.price) <= PRICE_TOLERANCE):
                    matched_existing.add(ex.id)
                    matched_desired.add(i)
                    break

        to_cancel = [o for o in existing if o.id not in matched_existing]
        to_place = [d for i, d in enumerate(desired_grid) if i not in matched_desired]

        # Cancel stale -- remove from local state immediately
        cancel_tasks = []
        for o in to_cancel:
            self.local_orders.pop(o.id, None)
            cancel_tasks.append(self._safe_cancel(o.id))

        # Place new -- register in local state on HTTP response
        place_tasks = []
        for o in to_place:
            place_tasks.append(self._safe_place(o))

        # Run all cancels and places concurrently
        all_tasks = cancel_tasks + place_tasks
        if all_tasks:
            results = await asyncio.gather(*all_tasks, return_exceptions=True)
        else:
            results = []

        placed = 0
        for i, result in enumerate(results):
            if i >= len(cancel_tasks) and not isinstance(result, (Exception, BaseException)) and result is not None:
                placed += 1

        return {"canceled": len(to_cancel), "placed": placed}

    async def _safe_cancel(self, order_id: str) -> None:
        try:
            await self.client.cancel_order(order_id)
        except Exception:
            pass

    async def _safe_place(self, o: OrderSpec) -> Optional[str]:
        try:
            resp = await self.client.create_and_post_order(
                OrderArgs(
                    token_id=o.token_id,
                    price=o.price,
                    side=o.side,
                    size=o.size,
                    expiration=0,
                ),
            )
            order_id = None
            if isinstance(resp, dict):
                order_id = resp.get("order_id") or resp.get("id")
            if order_id:
                tid = normalize_token_id(o.token_id)
                if tid:
                    self.local_orders[order_id] = TrackedOrder(
                        id=order_id,
                        token_id=tid,
                        side=o.side.upper(),
                        price=o.price,
                        size=o.size,
                    )
            return order_id
        except Exception:
            return None

    # ------------------------------------------------------------------
    # Requote
    # ------------------------------------------------------------------

    async def requote(self, reason: str) -> None:
        if not self.active_market or self.current_price <= 0:
            return
        if self.active_market.open_price <= 0:
            return
        if self.requoting:
            return
        self.requoting = True

        try:
            from datetime import datetime, timezone
            close_ms = datetime.fromisoformat(
                self.active_market.close_time.replace("Z", "+00:00")
            ).timestamp() * 1000.0
            tau_secs = max(0.0, (close_ms - time.time() * 1000.0) / 1000.0)
            q = int(self.inventory.yes_tokens - self.inventory.no_tokens)

            quotes = compute_quotes(self.current_price, self.active_market.open_price, tau_secs, q)

            # Only skip if reservation hasn't moved enough
            if reason == "tick" and abs(quotes.r_x - self.last_reservation) < REQUOTE_THRESHOLD_LOGIT:
                return

            # Apply toxicity multiplier to spread if adverse selection detected
            if self.toxicity_active:
                widened = QuoteResult(
                    fair_value=quotes.fair_value,
                    yes_bid=quotes.yes_bid,
                    yes_ask=quotes.yes_ask,
                    sigma_b=quotes.sigma_b,
                    gamma=quotes.gamma,
                    k=quotes.k,
                    Q_MAX=quotes.Q_MAX,
                    c1=quotes.c1,
                    c2=quotes.c2,
                    half_spread=quotes.half_spread,
                    r_x=quotes.r_x,
                )
                widening_logit = math.log(TOXICITY_MULTIPLIER)
                widened.yes_bid = math.floor(sigmoid(logit(quotes.yes_bid) - widening_logit) * 100) / 100.0
                widened.yes_ask = math.ceil(sigmoid(logit(quotes.yes_ask) + widening_logit) * 100) / 100.0
                grid = build_grid(
                    self.active_market.yes_token_id,
                    self.active_market.no_token_id,
                    widened, self.inventory,
                )
            else:
                grid = build_grid(
                    self.active_market.yes_token_id,
                    self.active_market.no_token_id,
                    quotes, self.inventory,
                )

            result = await self.converge_orders(grid)
            canceled = result["canceled"]
            placed = result["placed"]

            if len(grid) == 0 and canceled == 0:
                self.orders_live = False
                return
            self.last_reservation = quotes.r_x
            self.orders_live = placed > 0

            tau_min = round(tau_secs / 60)
            spread = quotes.yes_ask - quotes.yes_bid
            tox_str = " TOXIC" if self.toxicity_active else ""
            log(f"  {reason}: fv={quotes.fair_value:.3f} bid={quotes.yes_bid:.2f} "
                f"ask={quotes.yes_ask:.2f} spread={spread:.2f} q={q} Q={quotes.Q_MAX} "
                f"+{placed}/-{canceled}/{len(grid)} ${self.current_price:.2f} {tau_min}m{tox_str}")
        except Exception as err:
            msg = str(err)
            if "429" not in msg:
                log(f"  Requote error: {msg[:80]}")
        finally:
            self.requoting = False

    # ------------------------------------------------------------------
    # Provision
    # ------------------------------------------------------------------

    async def provision_market(self, market: MarketInfo) -> None:
        if market.id in self.provisioned_markets:
            return
        self.provisioned_markets.add(market.id)
        self.active_market = market
        self.last_reservation = -999.0
        self.last_pyth_tick_ms = time.time() * 1000.0
        self.inventory.yes_tokens = 0.0
        self.inventory.no_tokens = 0.0

        from datetime import datetime, timezone
        close_ms = datetime.fromisoformat(
            market.close_time.replace("Z", "+00:00")
        ).timestamp() * 1000.0
        ttl = round((close_ms - time.time() * 1000.0) / 60_000)
        log(f"Provisioning {market.symbol} ({market.id[:8]}, {ttl}m, open=${market.open_price:.2f})")

        if market.open_price > 0:
            tau_secs = (close_ms - time.time() * 1000.0) / 1000.0
            quotes = compute_quotes(market.open_price, market.open_price, tau_secs, 0)
            grid = build_grid(market.yes_token_id, market.no_token_id, quotes, self.inventory)
            result = await self.converge_orders(grid)
            placed = result["placed"]
            log(f"  Initial: {placed}/{len(grid)} orders, fv={quotes.fair_value:.3f} "
                f"bid={quotes.yes_bid:.2f} ask={quotes.yes_ask:.2f}")
            self.last_reservation = quotes.r_x
            self.orders_live = True
        else:
            log("  Open price not yet available -- will place grid on first Pyth tick")

    # ------------------------------------------------------------------
    # WebSocket event handlers
    # ------------------------------------------------------------------

    def _on_market_status(self, event: Dict[str, Any]) -> None:
        """Handle market status events -- open_price_set and resolved."""
        status = (event.get("status") or "").lower()
        if status.startswith("open_price_set:"):
            log(f"Open price set: market={str(event.get('market_id', ''))[:8]}")
            if self._loop:
                asyncio.run_coroutine_threadsafe(self._handle_open_price_set(), self._loop)
        elif status == "resolved" and self.active_market and event.get("market_id") == self.active_market.id:
            log(f"Market {self.active_market.id[:8]} resolved -- clearing {len(self.local_orders)} local orders")
            self.local_orders.clear()
            self.active_market = None
            self.orders_live = False

    async def _handle_open_price_set(self) -> None:
        if self.client:
            m = await fetch_latest_market(self.client, SYMBOL)
            if m:
                await self.provision_market(m)

    def _on_price_tick(self, event: Dict[str, Any]) -> None:
        """Handle Pyth price ticks."""
        if event.get("asset_id") != SYMBOL:
            return
        prev_tick_ms = self.last_pyth_tick_ms
        self.current_price = float(event.get("price", "0"))
        self.last_pyth_tick_ms = time.time() * 1000.0

        if self._loop:
            asyncio.run_coroutine_threadsafe(
                self._handle_price_tick(prev_tick_ms), self._loop,
            )

    async def _handle_price_tick(self, prev_tick_ms: float) -> None:
        now_ms = self.last_pyth_tick_ms
        # Stale feed recovery
        if self.orders_live and prev_tick_ms > 0 and (now_ms - prev_tick_ms) > STALE_FEED_MS:
            gap = (now_ms - prev_tick_ms) / 1000.0
            log(f"  STALE FEED: gap of {gap:.1f}s -- cancelling stale orders before requote")
            if self.client:
                try:
                    await self.client.cancel_all()
                except Exception:
                    pass
            self.local_orders.clear()
            self.orders_live = False

        if self.current_price > 0:
            await self.requote("tick")

    def _on_order_fill(self, event: Dict[str, Any]) -> None:
        """Handle order fill events -- update inventory and local state."""
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._handle_order_fill(event), self._loop)

    async def _handle_order_fill(self, event: Dict[str, Any]) -> None:
        filled_size = float(event.get("filled_size", "0")) / 1e6
        price = float(event.get("price", "0"))
        asset_id = (event.get("asset_id") or "").lower()
        side = (event.get("side") or "").lower()

        # Update local order tracking
        order_id = event.get("order_id", "")
        remaining = float(event.get("remaining_size", "0")) / 1e6
        if remaining <= 0:
            self.local_orders.pop(order_id, None)
        else:
            tracked = self.local_orders.get(order_id)
            if tracked:
                tracked.size = remaining

        if side != "buy" or filled_size <= 0 or not self.active_market:
            return

        # Update inventory
        yes_hex = self.active_market.yes_token_id.replace("0x", "").lower()
        no_hex = self.active_market.no_token_id.replace("0x", "").lower()
        fill_side = ""
        if asset_id == yes_hex or asset_id == "0x" + yes_hex:
            self.inventory.yes_tokens += filled_size
            fill_side = "yes"
        elif asset_id == no_hex or asset_id == "0x" + no_hex:
            self.inventory.no_tokens += filled_size
            fill_side = "no"

        # Toxicity tracking
        if fill_side:
            self.record_fill_side(fill_side)

        # Edge accounting
        if self.active_market.open_price > 0 and self.current_price > 0:
            from datetime import datetime, timezone
            close_ms = datetime.fromisoformat(
                self.active_market.close_time.replace("Z", "+00:00")
            ).timestamp() * 1000.0
            tau_secs = max(0.0, (close_ms - time.time() * 1000.0) / 1000.0)
            current_quotes = compute_quotes(self.current_price, self.active_market.open_price, tau_secs, 0)
            self.record_edge(price, current_quotes.fair_value, side, filled_size)

        net = self.inventory.yes_tokens - self.inventory.no_tokens
        log(f"  Fill: {filled_size} {fill_side} @ {price:.2f} | "
            f"YES={self.inventory.yes_tokens} NO={self.inventory.no_tokens} net={net}")
        await self.requote("fill")

    def _on_order_cancelled(self, event: Dict[str, Any]) -> None:
        self.local_orders.pop(event.get("order_id", ""), None)

    def _on_order_rejected(self, event: Dict[str, Any]) -> None:
        self.local_orders.pop(event.get("order_id", ""), None)

    def _on_position_update(self, event: Dict[str, Any]) -> None:
        """Event-driven inventory reconciliation via position_update WS events."""
        if not self.active_market:
            return
        asset_id = (event.get("asset_id") or "").lower()
        balance_raw = event.get("balance", "0")
        try:
            balance = int(balance_raw) / 1e6
        except (ValueError, OverflowError):
            balance = float(balance_raw) / 1e6

        yes_hex = self.active_market.yes_token_id.replace("0x", "").lower()
        no_hex = self.active_market.no_token_id.replace("0x", "").lower()
        if asset_id == yes_hex and balance != self.inventory.yes_tokens:
            log(f"  Inventory sync (event): YES {self.inventory.yes_tokens} -> {balance}")
            self.inventory.yes_tokens = balance
        elif asset_id == no_hex and balance != self.inventory.no_tokens:
            log(f"  Inventory sync (event): NO {self.inventory.no_tokens} -> {balance}")
            self.inventory.no_tokens = balance

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    async def shutdown(self) -> None:
        if self.shutting_down:
            return
        self.shutting_down = True
        log("\nShutting down...")

        if self.price_ws:
            self.price_ws.stop()
        if self.user_ws:
            self.user_ws.stop()
        if self.market_ws:
            self.market_ws.stop()

        if self.client:
            try:
                await self.client.cancel_all()
            except Exception:
                pass
            try:
                await self.client.close()
            except Exception:
                pass

        log("Stopped.")

    # ------------------------------------------------------------------
    # Wallet onboarding (faucet + approvals)
    # ------------------------------------------------------------------

    async def _onboard_wallet(self) -> None:
        """Onboard the wallet: claim testnet USDC, get USDC permit approval, get CTF approval."""
        addr = self.client.address

        # Check current status
        try:
            status = await self.client.get_wallet_status(addr)
            usdc = int(status.get("usdc_balance", "0"))
            allowance = int(status.get("usdc_allowance", "0"))
            ctf_ok = status.get("ctf_approved", False)
            log(f"Wallet status: ${usdc / 1e6:.2f} USDC, allowance={allowance}, CTF={'OK' if ctf_ok else 'NO'}")
        except Exception:
            usdc, allowance, ctf_ok = 0, 0, False

        # Claim faucet USDC if balance is low
        if usdc < int(CAPITAL * 1e6):
            needed = max(1, int((CAPITAL * 1e6 - usdc) / 100e6))  # 100 USDC per claim
            for i in range(min(needed, 5)):  # max 5 claims
                try:
                    resp = await self.client.claim_faucet(addr)
                    log(f"Faucet claim {i+1}: +$100 USDC (tx: {resp.get('tx_hash', '?')[:10]}...)")
                except Exception as e:
                    log(f"Faucet claim {i+1} failed: {e}")
                    break
                if i < needed - 1:
                    import asyncio
                    await asyncio.sleep(13)  # 12s cooldown between claims

        # Get CTF approval (gasless, backend sponsors the ETH)
        if not ctf_ok:
            try:
                resp = await self.client.prefund_ctf_approval(addr)
                log(f"CTF approval: {resp}")
            except Exception as e:
                log(f"CTF approval failed: {e}")

        # Get USDC approval via EIP-2612 permit (gasless)
        if allowance < int(CAPITAL * 1e6):
            try:
                # Sign a permit for max approval
                from eth_account import Account
                from eth_account.messages import encode_typed_data
                import time as _time

                account = Account.from_key(self.client._private_key)
                exchange = self.client.exchange_address
                usdc_addr = "0x9f702fa37809C1cb4e023f78F801f180a1DF5C8E"  # Circle USDC on Base Sepolia
                deadline = str(2 ** 256 - 1)
                value = str(2 ** 256 - 1)

                # EIP-2612 Permit typed data
                domain = {
                    "name": "USD Coin",
                    "version": "2",
                    "chainId": 84532,
                    "verifyingContract": usdc_addr,
                }
                permit_types = {
                    "Permit": [
                        {"name": "owner", "type": "address"},
                        {"name": "spender", "type": "address"},
                        {"name": "value", "type": "uint256"},
                        {"name": "nonce", "type": "uint256"},
                        {"name": "deadline", "type": "uint256"},
                    ],
                }
                # Get nonce from wallet status (USDC nonce, not tx nonce)
                nonce = 0  # First permit for a new wallet
                message = {
                    "owner": addr,
                    "spender": exchange,
                    "value": int(value),
                    "nonce": nonce,
                    "deadline": int(deadline),
                }

                signable = encode_typed_data(domain, permit_types, message)
                signed = account.sign_message(signable)
                sig = signed.signature.hex()
                v = signed.v
                r = "0x" + sig[:64]
                s = "0x" + sig[64:128]

                resp = await self.client.relay_permit(
                    owner=addr,
                    spender=exchange,
                    value=value,
                    deadline=deadline,
                    v=v,
                    r=r,
                    s=s,
                )
                log(f"USDC permit: {resp}")
            except Exception as e:
                log(f"USDC permit failed: {e} — trading may fail without approval")

        # Final status check
        try:
            status = await self.client.get_wallet_status(addr)
            usdc = int(status.get("usdc_balance", "0"))
            allowance = int(status.get("usdc_allowance", "0"))
            ctf_ok = status.get("ctf_approved", False)
            log(f"Ready: ${usdc / 1e6:.2f} USDC, allowance={'OK' if allowance > 0 else 'NONE'}, CTF={'OK' if ctf_ok else 'NO'}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()

        log("=== Production Market Maker (Shaw & Dalen 2025) ===")
        log(f"Capital: ${CAPITAL} | Max Loss: ${MAX_LOSS} | Tick: ${TICK_SIZE}")
        log(f"sigma_S(annual): {SIGMA_S_ANNUAL} | sigma_S(per sec): {SIGMA_S_PER_SEC:.4e}")
        log(f"Symbol: {SYMBOL} | Levels: {LEVELS}")

        # Setup wallet — use MM_PRIVATE_KEY or generate a fresh one
        mm_key = os.environ.get("MM_PRIVATE_KEY", "")
        if not mm_key:
            # Generate a fresh wallet for this market maker session
            from eth_account import Account
            acct = Account.create()
            mm_key = acct.key.hex()
            log(f"Generated fresh wallet: {acct.address}")
            log("Set MM_PRIVATE_KEY to reuse this wallet across restarts")

        # Create async client
        self.client = AsyncClobClient(
            host=BACKEND_URL,
            key=mm_key,
        )
        log(f"Wallet: {self.client.address}")

        # Onboard: claim testnet USDC + get approvals
        await self._onboard_wallet()

        # Create or derive API credentials
        self.creds = await self.client.create_or_derive_api_creds()
        self.client.set_api_creds(self.creds)
        log("API credentials ready")

        # Cancel stale orders from previous session, then seed local state
        skip_cancel = os.environ.get("SKIP_CANCEL", "")
        if not skip_cancel:
            try:
                await self.client.cancel_all()
            except Exception:
                pass
        await self.seed_local_orders()

        # Setup WebSocket connections
        # Market WS -- public market events
        self.market_ws = BlinkMarketWs(WS_URL)
        self.market_ws.on_connected = lambda: (
            log("MarketWs connected"),
            self.market_ws.subscribe(["*"]),
        )
        self.market_ws.on_market_status = self._on_market_status

        # Price WS -- Pyth oracle price feeds
        self.price_ws = BlinkPriceWs(WS_URL)
        self.price_ws.on_price_tick = self._on_price_tick
        self.price_ws.on_connected = lambda: (
            self.price_ws.subscribe([SYMBOL]),
            log(f"PriceWs subscribed: {SYMBOL}"),
        )

        # User WS -- authenticated fills and order lifecycle.
        # Accepts, fills, rejects, cancellations and redemptions all
        # arrive as `notification` events demuxed by `kind`.  Position
        # state is pushed as a single `state_changed` snapshot after
        # every engine mutation.
        self.user_ws = BlinkUserWs(WS_URL, self.creds)
        self.user_ws.on_order_fill = self._on_order_fill
        self.user_ws.on_order_cancelled = self._on_order_cancelled
        self.user_ws.on_order_rejected = self._on_order_rejected
        self.user_ws.on_state_changed = self._on_position_update
        self.user_ws.on_authenticated = lambda: log("UserWs authenticated")

        # Start all WS connections
        self.market_ws.start()
        self.price_ws.start()
        self.user_ws.start()

        # Provision latest market
        market = await fetch_latest_market(self.client, SYMBOL)
        if market:
            await self.provision_market(market)

        # Register SIGINT/SIGTERM for graceful shutdown
        shutdown_event = asyncio.Event()

        def _signal_handler() -> None:
            shutdown_event.set()

        try:
            self._loop.add_signal_handler(signal.SIGINT, _signal_handler)
            self._loop.add_signal_handler(signal.SIGTERM, _signal_handler)
        except NotImplementedError:
            # Windows doesn't support add_signal_handler
            pass

        log("Market maker running. Press Ctrl+C to stop.")

        try:
            await shutdown_event.wait()
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            await self.shutdown()


# ============================================================================
# Entry point
# ============================================================================

def main() -> None:
    mm = MarketMaker()
    try:
        asyncio.run(mm.run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
