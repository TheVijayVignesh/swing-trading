"""Universe tests — offline by default (bundled snapshots); network-marked live check."""
from __future__ import annotations

import pytest

from sts.contracts import SymbolMeta
from sts.data import universe


def test_bundled_nifty200_snapshot_exists_and_large():
    metas, mtime = universe._load_membership_csv("nifty200_membership.csv")
    assert len(metas) >= 150, "bundled snapshot must carry >=150 verified symbols"
    assert mtime > 0
    assert all(m.yahoo_symbol == f"{m.symbol}.NS" for m in metas)
    assert all(isinstance(m, SymbolMeta) for m in metas)


def test_bundled_nifty50_snapshot_exists():
    metas, _ = universe._load_membership_csv("nifty50_membership.csv")
    assert len(metas) == 50
    assert {m.symbol for m in metas} <= {m.symbol for m in universe.get_nifty200()}


def test_get_nifty200_uses_cache_without_network(monkeypatch):
    def boom(*a, **k):  # any network attempt fails hard
        raise AssertionError("network must not be touched when cache is fresh")
    monkeypatch.setattr(universe.requests.Session, "get", boom)
    metas = universe.get_nifty200()
    assert len(metas) >= 150
    assert "RELIANCE" in {m.symbol for m in metas}
    assert "RELIANCE.NS" in {m.yahoo_symbol for m in metas}


def test_get_nifty200_falls_back_when_fetch_fails(monkeypatch):
    class FakeResp:
        status_code = 503
        text = ""
        def raise_for_status(self):
            raise RuntimeError("down")
    monkeypatch.setattr(universe.requests.Session, "get", lambda self, *a, **k: FakeResp())
    # force the refresh path: bundled file is fresh but we pretend TTL expired
    monkeypatch.setattr(universe, "FETCH_TTL_SECONDS", -1.0)
    monkeypatch.setattr(universe, "_write_membership_csv", lambda *a, **k: None)  # don't clobber bundle
    metas = universe.get_nifty200(force_refresh=True)
    assert len(metas) >= 150  # degraded to snapshot, never fabricated


def test_parse_symbols_from_csv_text(monkeypatch):
    csv_text = "Company Name,Industry,Symbol,Series,ISIN Code\nFoo Ltd.,IT,FOO,EQ,INE000\n"
    monkeypatch.setattr(
        universe, "_nse_session",
        lambda: type("S", (), {"get": lambda self, url, timeout=None: type("R", (), {
            "text": csv_text, "raise_for_status": lambda self: None})()})(),
    )
    assert universe.fetch_nse_index_symbols("http://x") == ["FOO"]


def test_unavailable_without_fetch_or_bundle(monkeypatch):
    monkeypatch.setattr(universe, "_REF_DIR_CANDIDATES", ())
    with pytest.raises(RuntimeError, match="unavailable"):
        universe._get_universe(url="http://x", filename="nope.csv", force_refresh=False)


@pytest.mark.network
def test_live_nse_fetch_and_yahoo_resolve():
    """Real NSE CSV fetch; symbols come from the official list."""
    syms = universe.fetch_nse_index_symbols(universe.NIFTY200_CSV_URL)
    assert len(syms) == 200
    assert "RELIANCE" in syms and "TCS" in syms


@pytest.mark.network
def test_live_get_nifty200_refresh_writes_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(universe, "_REF_DIR_CANDIDATES", (tmp_path,))
    metas = universe.get_nifty200(force_refresh=True)
    assert len(metas) == 200
    assert (tmp_path / "nifty200_membership.csv").exists()


@pytest.mark.network
def test_live_yahoo_symbol_resolution_batch():
    """Verify a sample of bundled symbols actually resolve on Yahoo (yfinance)."""
    import yfinance as yf
    metas = universe.get_nifty200()
    sample = [m.yahoo_symbol for m in metas[:20]]
    df = yf.download(sample, period="5d", interval="1d", progress=False, threads=False, group_by="ticker")
    resolved = sum(
        1 for s in sample
        if s in df.columns.get_level_values(0) and df[s]["Close"].dropna().shape[0] > 0
    )
    assert resolved == len(sample), f"{len(sample) - resolved} sample symbols failed to resolve"
