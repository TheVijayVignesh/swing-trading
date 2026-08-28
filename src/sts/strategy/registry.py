"""Strategy registry — id -> pure evaluate(ctx, params) callable."""
from __future__ import annotations

from typing import Any, Callable

from sts.strategy.pullback_v1 import StrategyContext
from sts.strategy import pullback_v1, random_k

StrategyFn = Callable[[StrategyContext, dict[str, Any] | None], list]

STRATEGIES: dict[str, StrategyFn] = {
    "pullback-v1": pullback_v1.evaluate,
    "random-k": random_k.evaluate,
}


def get_strategy(strategy_id: str) -> StrategyFn:
    if strategy_id not in STRATEGIES:
        raise KeyError(f"unknown strategy_id {strategy_id!r}; known: {sorted(STRATEGIES)}")
    return STRATEGIES[strategy_id]
