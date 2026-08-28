"""random-k baseline: IDENTICAL detection to pullback-v1, then a seeded
uniform sample of k candidates. Isolates the value of the *selection* rules
from the detection rules (ablation control).
"""
from __future__ import annotations

import random
from typing import Any

from sts.strategy.pullback_v1 import StrategyContext, detect_candidates


def evaluate(ctx: StrategyContext, params: dict[str, Any] | None = None) -> list:
    p = dict(params or {})
    k = int(p.pop("k", 3))
    all_candidates = detect_candidates(ctx, p)
    if len(all_candidates) <= k:
        return list(all_candidates)
    seed = p.get("seed", ctx.rng_seed)
    rng = random.Random(seed)
    return rng.sample(all_candidates, k)
