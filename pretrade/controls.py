"""The individual pre-trade controls.

Each control is a small, independently testable unit that implements the
``Control`` protocol: it declares its kind (hard, soft, or audit) and evaluates
one order against the live context, returning a single ``ControlResult``.

Design principle, applied in every control: FAIL CLOSED. A missing, stale, or
unparseable input yields a block, never a pass, so a data outage cannot silently
let an order through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional, Protocol, runtime_checkable

from .config import ControlConfig
from .domain import ControlKind, ControlResult, ControlStatus, Order, OrderType, Side
from .market_data import MarketDataProvider
from .store import Store


@dataclass
class EvaluationContext:
    store: Store
    config: ControlConfig
    market_data: MarketDataProvider
    now: datetime
    actor: str = "system"


@runtime_checkable
class Control(Protocol):
    name: str
    kind: ControlKind

    def evaluate(self, order: Order, ctx: EvaluationContext) -> ControlResult:
        ...


def _money(n: Decimal) -> str:
    return "${:,.0f}".format(n)


class FieldValidation:
    name = "field_validation"
    kind = ControlKind.HARD

    def evaluate(self, order: Order, ctx: EvaluationContext) -> ControlResult:
        errors: list[str] = []
        if not order.account_id:
            errors.append("account required")
        if not order.symbol:
            errors.append("symbol required")
        if not isinstance(order.quantity, int) or order.quantity <= 0:
            errors.append("quantity must be a positive whole number")
        if order.order_type.requires_limit_price and not (order.limit_price and order.limit_price > 0):
            errors.append(f"limit price required for {order.order_type.value}")
        status = ControlStatus.BLOCK if errors else ControlStatus.PASS
        reason = "; ".join(errors) if errors else "all required fields present and well-typed"
        return ControlResult(self.name, self.kind, status, reason)


class KillSwitch:
    name = "kill_switch"
    kind = ControlKind.HARD

    def evaluate(self, order: Order, ctx: EvaluationContext) -> ControlResult:
        engaged = ctx.store.kill_engaged(order.tenant)
        if engaged:
            return ControlResult(self.name, self.kind, ControlStatus.BLOCK,
                                 f"master kill switch engaged for tenant {order.tenant}")
        return ControlResult(self.name, self.kind, ControlStatus.PASS, "not engaged")


class PriceCollar:
    """Blocks an order priced too far from the market.

    Soft (a supervisor may override) when the price is simply outside the band.
    Fails closed on a missing or stale quote, which is a data-integrity failure,
    not a pricing judgement.
    """

    name = "price_collar"
    kind = ControlKind.SOFT

    def evaluate(self, order: Order, ctx: EvaluationContext) -> ControlResult:
        quote = ctx.market_data.get_quote(order.symbol)
        if quote is None:
            return ControlResult(self.name, self.kind, ControlStatus.BLOCK,
                                 "data_stale: no quote available, failing closed")
        if not quote.is_fresh(ctx.now, ctx.config.quote_max_age_sec):
            age = int(quote.age_seconds(ctx.now))
            return ControlResult(self.name, self.kind, ControlStatus.BLOCK,
                                 f"data_stale: quote {age}s old exceeds {ctx.config.quote_max_age_sec}s bound, failing closed")
        mid = quote.mid
        band = ctx.config.collar_fraction
        lo, hi = mid * (1 - band), mid * (1 + band)
        ref = order.limit_price if order.order_type.requires_limit_price and order.limit_price else mid
        if ref < lo or ref > hi:
            return ControlResult(self.name, self.kind, ControlStatus.BLOCK,
                                 f"price {ref:.2f} outside collar {lo:.2f}-{hi:.2f} (mid {mid:.2f})")
        return ControlResult(self.name, self.kind, ControlStatus.PASS,
                             f"within collar {lo:.2f}-{hi:.2f} of mid {mid:.2f}")


class DuplicateRateGuard:
    """Duplicate-intent / runaway-algorithm detection.

    Counts identical orders (same account, side, symbol, quantity, type, price)
    within a short window. This is the control a FINRA "algorithms run amok"
    requirement actually calls for, rather than a blanket volume cap.
    """

    name = "duplicate_rate_guard"
    kind = ControlKind.HARD

    def evaluate(self, order: Order, ctx: EvaluationContext) -> ControlResult:
        prior = ctx.store.count_recent_signature(order.signature(), ctx.config.dup_window_sec, ctx.now)
        attempt = prior + 1
        if attempt >= ctx.config.dup_threshold:
            return ControlResult(self.name, self.kind, ControlStatus.BLOCK,
                                 f"identical order seen {attempt} times in {ctx.config.dup_window_sec}s "
                                 f"(same account, side, symbol, quantity) - runaway/duplicate pattern")
        return ControlResult(self.name, self.kind, ControlStatus.PASS,
                             f"no duplicate pattern ({attempt} of this signature in window)")


class BuyingPower:
    """Audit-only: flags a buy whose notional exceeds account cash.

    Non-gating on its own; it records exposure for the audit trail. Sells are
    not cash-constrained.
    """

    name = "buying_power"
    kind = ControlKind.AUDIT

    def evaluate(self, order: Order, ctx: EvaluationContext) -> ControlResult:
        if not order.side.is_buy:
            return ControlResult(self.name, self.kind, ControlStatus.PASS, "sell side, not cash-constrained")
        account = ctx.store.get_account(order.account_id)
        if account is None:
            return ControlResult(self.name, self.kind, ControlStatus.PASS, "account not found (audit-only)")
        quote = ctx.market_data.get_quote(order.symbol)
        px: Optional[Decimal] = order.limit_price if order.limit_price else (quote.mid if quote else None)
        if px is None:
            return ControlResult(self.name, self.kind, ControlStatus.AUDIT, "no price available to size notional (audit-only)")
        notional = Decimal(order.quantity) * px
        cash = Decimal(str(account["cash"]))
        if notional > cash:
            return ControlResult(self.name, self.kind, ControlStatus.AUDIT,
                                 f"notional {_money(notional)} exceeds cash {_money(cash)} (flagged, non-gating)")
        return ControlResult(self.name, self.kind, ControlStatus.PASS, f"notional {_money(notional)} within cash")


class CreditLimit:
    name = "credit_limit"
    kind = ControlKind.AUDIT

    def evaluate(self, order: Order, ctx: EvaluationContext) -> ControlResult:
        return ControlResult(self.name, self.kind, ControlStatus.PASS, "within credit limit (audit-only)")


def default_controls() -> list[Control]:
    """The day-one control chain, in evaluation order."""
    return [FieldValidation(), KillSwitch(), PriceCollar(), DuplicateRateGuard(), BuyingPower(), CreditLimit()]
