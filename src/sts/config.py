"""Session configuration (pydantic) + risk-profile presets + content hash.

Normative defaults come from ARCHITECTURE.md §7/§11 and ARCHITECTURE_V1.1 §11
(small-account tier: equity < ₹50k => 1.5% risk, 33% position cap, ₹4k notional).

SIZING-ENVELOPE MATH (audit v2, binding):
A candidate with daily ATR fraction `f` (ATR/price) sized by the risk engine has
    stop distance = trail_mult_atr * f * price      (trail_mult_atr default 1.5)
    qty           = floor(risk_per_trade * E / stop_distance)
    notional      ≈ risk_per_trade * E / (trail_mult_atr * f)
so a profile is FEASIBLE for ATR fraction f on capital E iff BOTH:
    position cap : notional <= max_position_pct * E   =>  f >=  risk / (mult * cap)
    min notional : notional >= min_notional           =>  f <=  risk * E / (mult * min_notional)
With mult = 1.5 and E = ₹25,000:
    standard (risk .01, cap .20, mn 5000): f ∈ [3.33% , 3.33%]  -> DEGENERATE single
        point: essentially the whole universe is untradeable (the audit bug).
    small    (risk .015, cap .33, mn 4000): f ∈ [3.03% , 6.25%] -> only high-ATR names.
    micro    (risk .02,  cap .60, mn 3000): f ∈ [2.22% , 11.11%] -> covers the
        volatile half of a NIFTY200-style universe.
Worked example (micro, RELIANCE @ ₹1310, ATR 1.5% => f=1.5% < 2.22% lower bound):
risk ₹500 / stop ₹29.5 -> qty 16, notional ₹20,960 > cap 0.60*25000=₹15,000 =>
position_cap VETO. That is HONEST: a ₹25k account cannot hold low-ATR large caps
inside any sane cap; the micro tier guarantees feasibility for f >= ~2.2%
instead of pretending every symbol is tradeable. `micro` is auto-selected at
create time when capital_initial < MICRO_TIER_CAPITAL_THRESHOLD unless the user
explicitly set risk_profile.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, Field

# ------------------------------------------------------------- presets (V1.1 §11 + audit v2)
RISK_PROFILES: dict[str, dict[str, float]] = {
    "small": {"risk_per_trade": 0.015, "max_position_pct": 0.33, "min_notional": 4000.0},
    "standard": {"risk_per_trade": 0.01, "max_position_pct": 0.20, "min_notional": 5000.0},
    "micro": {"risk_per_trade": 0.02, "max_position_pct": 0.60, "min_notional": 3000.0},
}

RiskProfile = Literal["small", "standard", "micro"]

# Sessions below this capital default to the `micro` profile (see module docstring);
# an explicitly-provided risk_profile always wins.
MICRO_TIER_CAPITAL_THRESHOLD = 30_000.0


def default_risk_profile_for(capital_initial: float, explicit: bool) -> str | None:
    """`micro` for small accounts unless the caller explicitly chose a profile."""
    if explicit or capital_initial >= MICRO_TIER_CAPITAL_THRESHOLD:
        return None
    return "micro"
OnStopPolicy = Literal["FLATTEN", "HOLD"]
ModeCfg = Literal["paper", "sandbox", "live"]


class SessionConfig(BaseModel):
    name: str
    capital_initial: float = Field(gt=0)
    mode: ModeCfg = "paper"
    universe: str = "NIFTY200"
    strategy_id: str = "pullback-v1"
    risk_profile: RiskProfile = "standard"
    ml_enabled: bool = False
    on_stop_policy: OnStopPolicy = "FLATTEN"
    params: dict[str, Any] = Field(default_factory=dict)

    # ---- common V1 defaults (ARCHITECTURE §7/§11); overridable via `params`
    max_positions: int = 4
    max_total_open_risk: float = 0.02
    max_gross_exposure: float = 0.80
    daily_loss_limit: float = 0.03
    drawdown_kill: float = 0.10
    time_stop_days: int = 10
    trail_mult_atr: float = 1.5
    t1_multiple: float = 1.0
    t2_multiple: float = 3.0

    # ---- effective values: explicit params override preset/common defaults
    @property
    def risk_per_trade(self) -> float:
        return float(self.params.get("risk_per_trade", RISK_PROFILES[self.risk_profile]["risk_per_trade"]))

    @property
    def max_position_pct(self) -> float:
        return float(self.params.get("max_position_pct", RISK_PROFILES[self.risk_profile]["max_position_pct"]))

    @property
    def min_notional(self) -> float:
        return float(self.params.get("min_notional", RISK_PROFILES[self.risk_profile]["min_notional"]))

    def effective(self, key: str, default: Any = None) -> Any:
        if key in self.params:
            return self.params[key]
        return getattr(self, key, default)


def content_hash(config: SessionConfig) -> str:
    """Stable sha256 hex of the canonical JSON dump."""
    payload = json.dumps(config.model_dump(), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ minimal YAML
# Flat scalar mapping + one nested `params` mapping — enough for this config,
# no external yaml dependency. Values: str/int/float/bool.

_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d*([eE][+-]?\d+)?$|^-?\d+[eE][+-]?\d+$")


def _emit_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return repr(v)
    s = str(v)
    if s == "" or any(c in s for c in ":#'\"\n") or s != s.strip():
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _parse_value(raw: str) -> Any:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == '"' and raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    if raw in ("true", "True"):
        return True
    if raw in ("false", "False"):
        return False
    if raw in ("null", "~", ""):
        return None
    if _INT_RE.match(raw):
        return int(raw)
    if _FLOAT_RE.match(raw):
        return float(raw)
    return raw


def to_yaml(config: SessionConfig) -> str:
    d = config.model_dump()
    lines: list[str] = []
    for k, v in d.items():
        if k == "params":
            lines.append("params:")
            for pk, pv in v.items():
                lines.append(f"  {pk}: {_emit_value(pv)}")
        else:
            lines.append(f"{k}: {_emit_value(v)}")
    return "\n".join(lines) + "\n"


def from_yaml(text: str) -> SessionConfig:
    top: dict[str, Any] = {}
    params: dict[str, Any] = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indented = line[0] in (" ", "\t")
        key, _, val = line.strip().partition(":")
        key, val = key.strip(), val.strip()
        if indented:
            params[key] = _parse_value(val)
        elif key == "params":
            continue
        else:
            top[key] = _parse_value(val)
    if params:
        top["params"] = params
    return SessionConfig(**top)
