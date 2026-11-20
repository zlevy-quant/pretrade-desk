"""Risk thresholds and control configuration.

In production these values live in a parameter store and are versioned and
change-controlled per tenant and environment. Here they are a single typed
object with conservative defaults.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ControlConfig:
    # Price collar half-width, in basis points, around the quote mid.
    collar_bps: int = 300
    # Maximum quote age, in seconds, before the collar fails closed.
    quote_max_age_sec: int = 60
    # Window and count for duplicate-intent detection.
    dup_window_sec: int = 60
    dup_threshold: int = 3

    @property
    def collar_fraction(self) -> Decimal:
        return Decimal(self.collar_bps) / Decimal(10_000)
