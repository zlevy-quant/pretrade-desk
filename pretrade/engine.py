"""The pre-trade engine: the single enforcement boundary.

Runs the control chain over an order and composes the per-control results into a
final verdict. Composition rule:

    any HARD control blocks   -> BLOCK, not overridable
    else any SOFT control blocks -> BLOCK, overridable (supervisor may release)
    else                      -> ALLOW

The engine is pure with respect to its inputs: given the same order and context
it returns the same verdict, which is what makes it straightforward to test and
to benchmark.
"""

from __future__ import annotations

from typing import Sequence

from .controls import Control, EvaluationContext, default_controls
from .domain import ControlKind, Decision, Order, Verdict


class PreTradeEngine:
    def __init__(self, controls: Sequence[Control] | None = None) -> None:
        self.controls: tuple[Control, ...] = tuple(controls if controls is not None else default_controls())

    def evaluate(self, order: Order, ctx: EvaluationContext) -> Verdict:
        results = tuple(c.evaluate(order, ctx) for c in self.controls)
        hard_block = any(r.kind == ControlKind.HARD and r.is_block for r in results)
        soft_block = any(r.kind == ControlKind.SOFT and r.is_block for r in results)
        if hard_block:
            return Verdict(Decision.BLOCK, overridable=False, controls=results)
        if soft_block:
            return Verdict(Decision.BLOCK, overridable=True, controls=results)
        return Verdict(Decision.ALLOW, overridable=False, controls=results)
