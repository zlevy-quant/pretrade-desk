"""Core value types for the pre-trade risk domain.

These types are deliberately transport- and storage-agnostic: the engine and the
controls operate on them, and the persistence and HTTP layers translate to and
from them at the edges. Monetary and price fields use Decimal so that currency
arithmetic is exact rather than binary-floating-point approximate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"
    SELL_SHORT = "sell_short"
    BUY_TO_COVER = "buy_to_cover"

    @property
    def is_buy(self) -> bool:
        return self in (Side.BUY, Side.BUY_TO_COVER)


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"

    @property
    def requires_limit_price(self) -> bool:
        return self in (OrderType.LIMIT, OrderType.STOP_LIMIT)


class ControlKind(str, Enum):
    """How a control's outcome composes into the final verdict.

    HARD  gating and final; a block cannot be overridden.
    SOFT  gating but releasable by an authorized supervisor.
    AUDIT informational; recorded but never blocks on its own.
    """

    HARD = "hard"
    SOFT = "soft"
    AUDIT = "audit"


class ControlStatus(str, Enum):
    PASS = "pass"
    BLOCK = "block"
    AUDIT = "audit"   # flagged by an audit-only control, non-gating
    SKIP = "skip"     # not evaluated (for example, order already invalid)


class Decision(str, Enum):
    ALLOW = "allow"
    BLOCK = "block"


@dataclass(frozen=True)
class Order:
    """An order presented to the gateway for a pre-trade decision."""

    account_id: str
    symbol: str
    side: Side
    order_type: OrderType
    quantity: int
    limit_price: Optional[Decimal] = None
    tenant: str = "RIA"

    def signature(self) -> str:
        """Identity used for duplicate-intent detection.

        Two orders with the same account, side, instrument, quantity, type, and
        limit price express the same trading intent; a burst of them is the
        runaway/duplicate pattern the rate guard is meant to catch.
        """
        lp = "" if self.limit_price is None else str(self.limit_price)
        return "|".join(
            [self.account_id, self.side.value, self.symbol, str(self.quantity), self.order_type.value, lp]
        )


@dataclass(frozen=True)
class ControlResult:
    name: str
    kind: ControlKind
    status: ControlStatus
    reason: str

    @property
    def is_block(self) -> bool:
        return self.status == ControlStatus.BLOCK


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    overridable: bool
    controls: tuple[ControlResult, ...] = field(default_factory=tuple)

    @property
    def allowed(self) -> bool:
        return self.decision == Decision.ALLOW

    def blocking_controls(self) -> tuple[ControlResult, ...]:
        return tuple(c for c in self.controls if c.is_block)
