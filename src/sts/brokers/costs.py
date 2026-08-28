"""Transaction cost engine — India equity delivery, driven by configs/costs.yaml.

Money is Decimal-rounded to paise (ROUND_HALF_UP) per component; total is the
exact sum of rounded components so ledger arithmetic balances to the paisa.

Golden totals (schedule c1.0.0):
    BUY  10 @ 1000 -> exchange 0.30, sebi 0.01, gst 0.06, stamp 1.50, total   1.87
    SELL 10 @ 1010 -> stt 10.10, exchange 0.30, sebi 0.01, gst 0.06, dp 13.00,
                      total 23.47
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from pathlib import Path

from sts.contracts import Side

_PAISE = Decimal("0.01")


def round_paise(x: float, *, down: bool = False) -> float:
    """Round to paise (2dp). `down=True` floors (used for stop fills — never optimistic)."""
    mode = ROUND_DOWN if down else ROUND_HALF_UP
    return float(Decimal(repr(x)).quantize(_PAISE, rounding=mode))


@dataclass(frozen=True, slots=True)
class CostSchedule:
    """Parsed costs.yaml with a content hash pinning the exact schedule used."""

    version: str = "c1.0.0"
    brokerage_per_order: float = 0.0
    stt_sell_pct: float = 0.001
    exchange_txn_pct: float = 0.0000297
    gst_on_txn_pct: float = 0.18
    sebi_per_crore: float = 10.0
    stamp_buy_pct: float = 0.00015
    dp_charge_sell_flat: float = 13.0
    content_hash: str = ""


def load_cost_schedule(path: str | Path) -> CostSchedule:
    """Load a flat `key: value` YAML file without external dependencies."""
    raw = Path(path).read_text(encoding="utf-8")
    content_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    values: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        key, _, val = line.partition(":")
        values[key.strip()] = val.strip()
    known = {f.name for f in fields(CostSchedule)}
    kwargs = {}
    for key, val in values.items():
        if key == "content_hash" or key not in known:
            continue
        kwargs[key] = val if key == "version" else float(val)
    return CostSchedule(content_hash=content_hash, **kwargs)


def compute_costs(side: Side, px: float, qty: int, sched: CostSchedule) -> dict[str, float]:
    """Per-trade cost breakdown in rupees, every component rounded to paise.

    Unit convention: `*_pct` schedule fields are FRACTIONS of turnover
    (stt_sell_pct 0.001 == 0.1%, exchange_txn_pct 0.0000297 == 0.00297%);
    gst_on_txn_pct is a rate multiplier on its base (0.18 == 18%).
    GST base = brokerage + raw exchange txn + raw SEBI fee.
    Stamp duty applies to BUY turnover only; STT and DP charge to SELL only.
    """
    turnover = px * qty
    brokerage = round_paise(sched.brokerage_per_order)
    stt = round_paise(turnover * sched.stt_sell_pct) if side is Side.SELL else 0.0
    exch_raw = turnover * sched.exchange_txn_pct
    sebi_raw = turnover * (sched.sebi_per_crore / 1e7)
    gst_raw = (brokerage + exch_raw + sebi_raw) * sched.gst_on_txn_pct

    breakdown = {
        "brokerage": brokerage,
        "stt": stt,
        "exchange_txn": round_paise(exch_raw),
        "sebi": round_paise(sebi_raw),
        "gst": round_paise(gst_raw),
        "stamp_duty": round_paise(turnover * sched.stamp_buy_pct) if side is Side.BUY else 0.0,
        "dp_charge": round_paise(sched.dp_charge_sell_flat) if side is Side.SELL else 0.0,
    }
    breakdown["total"] = round_paise(sum(breakdown.values()))
    return breakdown
