"""Engine latency benchmark.

The pre-trade engine runs in line on the order path, so its latency is a
first-class property. This measures the per-order evaluation latency of the full
control chain against an in-memory store, which isolates the engine and control
logic (the database path is exercised separately in integration).

Run: python -m bench.latency [iterations]
"""

from __future__ import annotations

import sys
import time
from decimal import Decimal

from pretrade.controls import EvaluationContext, default_controls
from pretrade.domain import Order, OrderType, Side
from pretrade.engine import PreTradeEngine
from pretrade.market_data import utcnow
from pretrade.store import Store


def percentile(sorted_us: list[float], p: float) -> float:
    if not sorted_us:
        return 0.0
    k = min(len(sorted_us) - 1, int(round((p / 100.0) * (len(sorted_us) - 1))))
    return sorted_us[k]


def main() -> None:
    iterations = int(sys.argv[1]) if len(sys.argv) > 1 else 20_000
    store = Store(":memory:")
    store.seed_if_empty()
    engine = PreTradeEngine(default_controls())
    cfg = store.get_config()

    # A representative in-collar limit order that exercises every control.
    order = Order(account_id="ACME-0042", symbol="AAPL", side=Side.BUY,
                  order_type=OrderType.LIMIT, quantity=100, limit_price=Decimal("228.00"))

    # Warm up.
    for _ in range(1000):
        engine.evaluate(order, EvaluationContext(store, cfg, store, utcnow()))

    samples: list[float] = []
    t0 = time.perf_counter()
    for _ in range(iterations):
        now = utcnow()
        s = time.perf_counter()
        engine.evaluate(order, EvaluationContext(store, cfg, store, now))
        samples.append((time.perf_counter() - s) * 1e6)  # microseconds
    wall = time.perf_counter() - t0

    samples.sort()
    print(f"iterations      {iterations:,}")
    print(f"throughput      {iterations / wall:,.0f} orders/sec")
    print(f"mean latency    {sum(samples) / len(samples):.1f} us")
    print(f"p50 latency     {percentile(samples, 50):.1f} us")
    print(f"p95 latency     {percentile(samples, 95):.1f} us")
    print(f"p99 latency     {percentile(samples, 99):.1f} us")
    print(f"max latency     {samples[-1]:.1f} us")


if __name__ == "__main__":
    main()
