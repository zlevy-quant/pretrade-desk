"""
pretrade: a pre-trade risk control engine.

A compact, dependency-free reference implementation of the control layer that
sits between order entry and the market on a multi-tenant brokerage trading
platform. Every order is evaluated by a chain of controls before it is accepted,
and the engine is designed to fail closed: a missing, stale, or unparseable
input produces a block rather than a pass.

The package is organized so the risk logic is independent of transport and
storage:

    domain       value types: Side, OrderType, Order, Verdict, ControlResult
    config       risk limits and control thresholds
    market_data  quote model and the freshness contract
    store        SQLite-backed repositories (orders, audit, accounts, quotes)
    controls     one evaluatable unit per control, each declaring hard/soft/audit
    engine       orchestrates the controls and composes the final verdict
    audit        append-only (write-once) decision and approval trail
    service      a thin JSON/HTTP surface over the engine (optional)

This is a personal, generalized reimplementation of pre-trade control patterns.
It contains no employer code, data, or secrets.
"""

__all__ = ["__version__"]
__version__ = "1.0.0"
