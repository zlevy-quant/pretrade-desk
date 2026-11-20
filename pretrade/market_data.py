"""Market-data model and the freshness contract used by the price collar.

The freshness rule is the core risk decision here: a quote is only usable if it
is recent enough, and a two-sided quote is only as fresh as its *stalest* side.
A missing or stale quote is never treated as acceptable; the caller fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional, Protocol


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: Decimal
    ask: Decimal
    as_of: datetime

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal(2)

    def age_seconds(self, now: datetime) -> float:
        return (now - self.as_of).total_seconds()

    def is_fresh(self, now: datetime, max_age_sec: int) -> bool:
        age = self.age_seconds(now)
        # A negative age (clock skew pushing the quote into the future) is not
        # trusted either; only a non-negative age within the bound is fresh.
        return 0 <= age <= max_age_sec


class MarketDataProvider(Protocol):
    def get_quote(self, symbol: str) -> Optional[Quote]:
        ...


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
