from types import SimpleNamespace

from sts.config import SessionConfig
from sts.portfolio.selector import ScoredCandidate, select


def cand(sym, score, risk, notional, price=100.0):
    stop = price * 0.95
    return ScoredCandidate(symbol=sym, score=score, entry_price=price, stop_px=stop,
                           qty=int(notional // price), risk_amount=risk,
                           notional=notional)


def flat_corr(a, b):
    return 0.1  # everything uncorrelated by default


EQUITY = 100_000.0
CFG = SessionConfig(name="t", capital_initial=EQUITY)


class TestSelector:
    def test_admits_in_score_order_until_cap(self):
        cands = [cand("A", 3.0, 600.0, 10_000.0),
                 cand("B", 2.0, 600.0, 10_000.0),
                 cand("C", 1.0, 600.0, 10_000.0)]
        selected, rej = select(cands, [], flat_corr,
                               lambda s: "BANK" if s in ("A", "B") else "IT",
                               EQUITY, CFG)
        # A,B same sector ok (2 <= 2); C would be a third distinct sector -> admitted too
        assert [c.symbol for c in selected] == ["A", "B", "C"]
        assert rej == []

    def test_correlation_rejection(self):
        def corr(a, b):
            return 0.9 if {a, b} == {"A", "B"} else 0.1
        cands = [cand("A", 2.0, 500.0, 5_000.0),
                 cand("B", 1.5, 500.0, 5_000.0),
                 cand("Z", 1.0, 500.0, 5_000.0)]
        selected, rej = select(cands, [], corr, lambda s: "X", EQUITY, CFG)
        # B too-correlated -> out; Z uncorrelated and sector X now holds 2 (<=2) -> in
        assert [c.symbol for c in selected] == ["A", "Z"]
        assert rej == [("B", "CORRELATION")]

    def test_nan_correlation_fails_closed(self):
        def nan_corr(a, b):
            return float("nan")
        cands = [cand("A", 1.0, 500.0, 5_000.0)]
        selected, rej = select(cands, [SimpleNamespace(symbol="OPEN1", risk_amount=100.0,
                                                       notional=5_000.0)],
                               nan_corr, lambda s: "X", EQUITY, CFG)
        assert selected == [] and rej == [("A", "CORRELATION")]

    def test_sector_count_cap(self):
        cands = [cand("A1", 4.0, 300.0, 3_000.0),
                 cand("A2", 3.0, 300.0, 3_000.0),
                 cand("A3", 2.0, 300.0, 3_000.0)]
        selected, rej = select(cands, [], flat_corr, lambda s: "PHARMA", EQUITY, CFG)
        assert [c.symbol for c in selected] == ["A1", "A2"]
        assert ("A3", "SECTOR_COUNT") in rej

    def test_sector_exposure_cap(self):
        # small tier: position cap 33% lets a single name reach 30k;
        # sector cap is still 40% of equity
        cfg_small = SessionConfig(name="s", capital_initial=EQUITY, risk_profile="small")
        big1 = cand("BIG1", 4.0, 300.0, 30_000.0)
        big2 = cand("BIG2", 3.0, 300.0, 12_000.0)   # sector total 42k > 40k
        selected, rej = select([big1, big2], [], flat_corr,
                               lambda s: "METAL", EQUITY, cfg_small)
        assert [c.symbol for c in selected] == ["BIG1"]
        assert ("BIG2", "SECTOR_EXPOSURE") in rej

    def test_max_positions_counts_open_positions(self):
        open_pos = [SimpleNamespace(symbol=f"O{i}", risk_amount=200.0, notional=5_000.0)
                    for i in range(4)]
        cands = [cand("NEW", 9.0, 300.0, 3_000.0)]
        selected, rej = select(cands, open_pos, flat_corr, lambda s: "AUTO",
                               EQUITY, CFG)
        assert selected == [] and rej == [("NEW", "MAX_POSITIONS")]

    def test_total_open_risk_with_existing(self):
        open_pos = [SimpleNamespace(symbol="O1", risk_amount=1_700.0, notional=10_000.0)]
        # existing 1700 + new 400 > 2% of 100k = 2000
        cands = [cand("RISKY", 2.0, 400.0, 5_000.0)]
        selected, rej = select(cands, open_pos, flat_corr, lambda s: "FMCG",
                               EQUITY, CFG)
        assert rej == [("RISKY", "TOTAL_OPEN_RISK")]

    def test_position_cap_against_equity(self):
        cands = [cand("WHALE", 2.0, 300.0, 25_000.0)]  # > 20% equity
        selected, rej = select(cands, [], flat_corr, lambda s: "ENERGY", EQUITY, CFG)
        assert rej == [("WHALE", "POSITION_CAP")]

    def test_duplicate_symbol_rejected_once(self):
        cands = [cand("DUP", 2.0, 300.0, 3_000.0), cand("DUP", 1.9, 300.0, 3_000.0)]
        selected, rej = select(cands, [], flat_corr, lambda s: "UTIL", EQUITY, CFG)
        assert [c.symbol for c in selected] == ["DUP"]
        assert ("DUP", "DUPLICATE") in rej

    def test_small_tier_profile_changes_admission(self):
        cfg_small = SessionConfig(name="s", capital_initial=EQUITY, risk_profile="small")
        cands = [cand("S", 1.0, 1_600.0, 30_000.0)]
        # standard: 1600 > 2%*100k? no (2000); but 30k > 20% cap -> rejected
        _, rej_std = select(cands, [], flat_corr, lambda s: "CHEM", EQUITY, CFG)
        # small: 30k <= 33% cap -> admitted
        sel_small, _ = select(cands, [], flat_corr, lambda s: "CHEM", EQUITY, cfg_small)
        assert rej_std == [("S", "POSITION_CAP")]
        assert [c.symbol for c in sel_small] == ["S"]
