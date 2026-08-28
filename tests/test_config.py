import pytest
from pydantic import ValidationError

from sts.config import RISK_PROFILES, SessionConfig, content_hash, from_yaml, to_yaml


def make_cfg(**kw) -> SessionConfig:
    base = dict(name="test-session", capital_initial=25000.0)
    base.update(kw)
    return SessionConfig(**base)


class TestDefaults:
    def test_common_defaults(self):
        cfg = make_cfg()
        assert cfg.mode == "paper"
        assert cfg.universe == "NIFTY200"
        assert cfg.strategy_id == "pullback-v1"
        assert cfg.risk_profile == "standard"
        assert cfg.ml_enabled is False
        assert cfg.on_stop_policy == "FLATTEN"
        assert cfg.max_positions == 4
        assert cfg.max_total_open_risk == 0.02
        assert cfg.max_gross_exposure == 0.80
        assert cfg.daily_loss_limit == 0.03
        assert cfg.drawdown_kill == 0.10
        assert cfg.time_stop_days == 10
        assert cfg.trail_mult_atr == 1.5
        assert cfg.t1_multiple == 1.0
        assert cfg.t2_multiple == 3.0

    def test_risk_profile_presets(self):
        small = make_cfg(risk_profile="small")
        std = make_cfg(risk_profile="standard")
        micro = make_cfg(risk_profile="micro")
        assert (small.risk_per_trade, small.max_position_pct, small.min_notional) == (0.015, 0.33, 4000.0)
        assert (std.risk_per_trade, std.max_position_pct, std.min_notional) == (0.01, 0.20, 5000.0)
        assert set(RISK_PROFILES) == {"small", "standard", "micro"}
        # audit v2: micro tier for capital < 30k (sizing-envelope fix)
        assert (micro.risk_per_trade, micro.max_position_pct, micro.min_notional) == (0.02, 0.60, 3000.0)

    def test_params_override_preset(self):
        cfg = make_cfg(risk_profile="small", params={"risk_per_trade": 0.02})
        assert cfg.risk_per_trade == 0.02
        assert cfg.min_notional == 4000.0  # untouched preset value

    def test_validation(self):
        with pytest.raises(ValidationError):
            make_cfg(capital_initial=-5)
        with pytest.raises(ValidationError):
            make_cfg(risk_profile="whale")
        with pytest.raises(ValidationError):
            make_cfg(on_stop_policy="YOLO")


class TestContentHash:
    def test_stable_and_sensitive(self):
        a = make_cfg()
        b = make_cfg()
        c = make_cfg(params={"risk_per_trade": 0.02})
        assert content_hash(a) == content_hash(b)
        assert len(content_hash(a)) == 64
        assert content_hash(a) != content_hash(c)
        assert content_hash(a) != content_hash(make_cfg(capital_initial=50000.0))


class TestYamlRoundtrip:
    def test_roundtrip_plain(self):
        cfg = make_cfg()
        again = from_yaml(to_yaml(cfg))
        assert again.model_dump() == cfg.model_dump()

    def test_roundtrip_full(self):
        cfg = make_cfg(
            risk_profile="small",
            ml_enabled=True,
            on_stop_policy="HOLD",
            params={"risk_per_trade": 0.02, "note": "hello world", "flag": False},
        )
        again = from_yaml(to_yaml(cfg))
        assert again.model_dump() == cfg.model_dump()

    def test_yaml_is_deterministic(self):
        cfg = make_cfg()
        assert to_yaml(cfg) == to_yaml(make_cfg())
