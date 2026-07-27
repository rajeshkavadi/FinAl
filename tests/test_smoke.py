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
    assert pf.total_invested == 2200000          # cost basis
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


def test_holding_period_classification():
    from analyzer.tax import AssetClass, is_long_term
    import datetime as dt
    asof = dt.date(2026, 7, 27)
    assert is_long_term(dt.date(2025, 1, 1), asof, 12) is True    # ~18 months
    assert is_long_term(dt.date(2026, 1, 1), asof, 12) is False   # ~7 months


def test_switch_tax_ltcg_exemption():
    from analyzer.tax import AssetClass, TaxLine, estimate_switch_tax
    import datetime as dt
    asof = dt.date(2026, 7, 27)
    # A long-term equity gain of 2,00,000: 1,25,000 exempt, 75,000 @12.5% + 4% cess
    line = TaxLine("X", AssetClass.EQUITY, invested=300000,
                   current_value=500000, buy_date=dt.date(2024, 1, 1))
    rep = estimate_switch_tax([line], asof=asof)
    assert rep.lines[0].term == "LTCG"
    assert round(rep.ltcg_taxable_after_exemption) == 75000
    assert round(rep.ltcg_tax) == round(75000 * 0.125 * 1.04)   # 9750


def test_switch_tax_stcg_rate():
    from analyzer.tax import AssetClass, TaxLine, estimate_switch_tax
    import datetime as dt
    asof = dt.date(2026, 7, 27)
    # Short-term gain 1,00,000 @20% + 4% cess = 20,800
    line = TaxLine("Y", AssetClass.EQUITY, invested=200000,
                   current_value=300000, buy_date=dt.date(2026, 3, 1))
    rep = estimate_switch_tax([line], asof=asof)
    assert rep.lines[0].term == "STCG"
    assert round(rep.stcg_tax) == round(100000 * 0.20 * 1.04)   # 20800


def test_switch_tax_from_analysis():
    _, a, sugs = _analyse()
    names = a.sell_candidates(sugs)
    assert names   # Beta NBFC (concentration) + micro positions + Theta (averaging)
    rep = a.switch_tax(names)
    assert rep.gross_proceeds > 0
    assert rep.total_tax >= 0


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
