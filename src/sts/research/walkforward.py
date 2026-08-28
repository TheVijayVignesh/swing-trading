"""Purged walk-forward splitting (ARCHITECTURE.md §11, V1.1 §8 discipline).

A fold is ((train_start, train_end), (test_start, test_end)) with INCLUSIVE
date bounds, where:
    - train window = the `train_days` sessions immediately BEFORE the embargo
      gap (rolling, not anchored);
    - embargo gap  = `embargo_days` sessions of pure separation between the
      last train session and the first test session — no train sample is ever
      temporally adjacent to a test sample;
    - test windows advance by exactly `test_days`, so test segments never
      overlap.

All arithmetic is positional on the sorted date index, so "days" means
TRADING sessions present in `dates`. Pure function; deterministic.
"""
from __future__ import annotations


def purged_split(
    dates,
    train_days: int,
    test_days: int,
    embargo_days: int,
) -> list[tuple[tuple, tuple]]:
    """Split sorted trading dates into purged (train, test) fold pairs.

    dates   : sequence of ordered session stamps (any comparable type).
    Returns list of ((train_first, train_last), (test_first, test_last)).
    """
    if train_days < 1 or test_days < 1 or embargo_days < 0:
        raise ValueError("train_days/test_days must be >=1, embargo_days >=0")
    ds = list(dates)
    n = len(ds)
    folds: list[tuple[tuple, tuple]] = []
    k = 0
    while True:
        test_start = train_days + embargo_days + k * test_days
        if test_start >= n:
            break
        test_end = min(test_start + test_days, n)          # exclusive
        train_end = test_start - embargo_days              # exclusive
        train_start = train_end - train_days
        k += 1
        if train_start < 0:
            continue
        folds.append((
            (ds[train_start], ds[train_end - 1]),
            (ds[test_start], ds[test_end - 1]),
        ))
    return folds
