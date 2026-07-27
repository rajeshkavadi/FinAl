"""Smoke tests: load the sample CSV, run analytics + rules, render a report.

Run with:  python -m pytest  (or)  python tests/test_smoke.py
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from analyzer.analytics import Analysis
from analyzer.loader import load
from analyzer.models import AssetType
from analyzer.report import build_dashboard
from analyzer.rules import run_rules

SAMPLE = ROOT / "sample_data" / "example_portfolio.csv"


def _analyse():
    pf = load(SAMPLE)
    a = Analysis(pf)
    return pf, a, run_rules(a)


def test_loads_holdings():
    pf, a, _ = _analyse()
    assert len(pf.equities()) == 10
    assert pf.total_invested == 2450000          # cost basis
    assert a.equity_total == 2515600             # marked to market (qty * price)


def test_concentration_flags_beta():
    pf, a, sugs = _analyse()
    # Beta NBFC is 700000/2450000 = 28.6% -> must raise a HIGH single-stock flag
    high = [s for s in sugs if s.rule == "single_stock_concentration" and s.severity == "high"]
    assert any("Beta NBFC" in s.title for s in high)


def test_duplicate_detection():
    _, _, sugs = _analyse()
    assert any(s.rule == "averaging_into_speculative" for s in sugs)


def test_high_risk_book_rule():
    _, a, sugs = _analyse()
    assert a.high_risk_pct > 0.4
    assert any(s.rule == "high_risk_book" for s in sugs)


def test_report_renders():
    pf, a, sugs = _analyse()
    html = build_dashboard(pf, a, sugs)
    assert "<!doctype html>" in html.lower()
    assert "Restructuring suggestions" in html


def test_pl_layer():
    pf, a, _ = _analyse()
    # every sample row has quantity + price -> full market coverage
    assert pf.fully_valued
    assert a.pl.has_data and a.pl.coverage == 1.0
    # Epsilon Cargo is the built-in loser (bought 400k, worth 325k)
    assert any(r.name == "Epsilon Cargo" and r.pl < 0 for r in a.pl.losers)
    # totals reconcile with per-row sums
    assert round(a.pl.total_pl) == round(a.pl.current_value - a.pl.invested)


def test_return_math():
    from analyzer.returns import cagr, xirr
    import datetime as dt
    # doubling in ~1 year ~ 100% CAGR
    c = cagr(100000, 200000, dt.date.today() - dt.timedelta(days=365))
    assert 0.98 < c < 1.02
    # simple XIRR: -100 today, +110 in a year -> ~10%
    r = xirr([(dt.date(2024, 1, 1), -100), (dt.date(2025, 1, 1), 110)])
    assert 0.09 < r < 0.11


def test_portfolio_xirr_runs():
    _, a, _ = _analyse()
    x = a.portfolio_xirr()   # sample has buy dates -> should produce a number
    assert x is not None


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
