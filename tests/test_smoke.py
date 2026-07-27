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
FUNDS = ROOT / "sample_data" / "example_funds.csv"
FUND_HOLDINGS = ROOT / "sample_data" / "example_fund_holdings.csv"


def _analyse():
    pf = load(SAMPLE)
    a = Analysis(pf)
    return pf, a, run_rules(a)


def _analyse_with_funds():
    from analyzer.models import AssetType
    from analyzer.loader import load_csv
    from analyzer.lookthrough import load_compositions
    pf = load(SAMPLE)
    pf.holdings.extend(load_csv(FUNDS, AssetType.MUTUAL_FUND).holdings)
    comps = load_compositions(FUND_HOLDINGS)
    a = Analysis(pf, compositions=comps)
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


def test_lookthrough_weights_normalise():
    from analyzer.lookthrough import load_compositions, norm
    comps = load_compositions(FUND_HOLDINGS)
    vega = comps[norm("Vega Technology Fund")]
    # weights given as percentages must become fractions
    assert abs(vega.named_weight - 0.55) < 1e-9   # 18+15+12+10 = 55%
    assert all(0 < c.weight < 1 for c in vega.holdings)


def test_lookthrough_combines_direct_and_fund():
    _, a, _ = _analyse_with_funds()
    lt = a.lookthrough
    assert lt is not None
    # Gamma Tech is held directly AND in both funds -> a direct+fund overlap
    gamma = next(e for e in lt.exposures if e.name == "Gamma Tech")
    assert gamma.direct > 0 and len(gamma.via_funds) == 2
    assert gamma.total > gamma.direct                     # look-through adds exposure
    assert gamma in lt.direct_and_fund_overlaps()
    assert gamma in lt.multi_fund_overlaps()


def test_lookthrough_rules_fire():
    _, _, sugs = _analyse_with_funds()
    rules = {s.rule for s in sugs}
    assert "lookthrough_direct_fund_overlap" in rules


def test_lookthrough_base_and_attribution():
    pf, a, _ = _analyse_with_funds()
    lt = a.lookthrough
    # base = direct equity value + fund value
    fund_val = sum(h.current_value for h in pf.funds())
    assert round(lt.total_base) == round(a.equity_total + fund_val)
    # matched + unattributed fund value == total fund value
    assert round(lt.matched_fund_value + lt.unattributed) == round(fund_val)


DISCLOSURE = ROOT / "sample_data" / "example_disclosure_orion.csv"


def test_parse_disclosure_filters_debt_and_scales():
    from analyzer.compositions_fetch import parse_disclosure
    fc = parse_disclosure(DISCLOSURE)
    names = {c.name for c in fc.holdings}
    # equity kept, debt/TREPS/receivables/total dropped
    assert "Gamma Tech Ltd" in names and "Alpha Bank Ltd" in names
    assert not any("GOI" in n or "TREPS" in n or "Total" in n for n in names)
    assert len(fc.holdings) == 5
    # 8.00% -> 0.08 fraction
    gamma = next(c for c in fc.holdings if c.name.startswith("Gamma"))
    assert abs(gamma.weight - 0.08) < 1e-9
    assert fc.fund and "Orion" in fc.fund


def test_fetch_compositions_cache_roundtrip(tmp_path=None):
    import tempfile
    from analyzer.compositions_fetch import (CompositionCache, discover_disclosures,
                                             fetch_compositions)
    from analyzer.lookthrough import norm
    d = tmp_path or Path(tempfile.mkdtemp())
    cache = CompositionCache(d / "comp_cache.json")
    disc = discover_disclosures(DISCLOSURE.parent)
    funds = list(disc.keys())
    comps = fetch_compositions(funds, disclosures=disc, cache=cache)
    assert comps                                   # resolved from disclosure file
    assert (d / "comp_cache.json").exists()
    # second call with an empty disclosures map must hit the cache
    cache2 = CompositionCache(d / "comp_cache.json")
    again = fetch_compositions(funds, cache=cache2)
    assert set(again.keys()) == set(comps.keys())


def test_fetched_compositions_feed_lookthrough():
    from analyzer.compositions_fetch import discover_disclosures, fetch_compositions
    from analyzer.loader import load_csv
    from analyzer.models import AssetType
    pf = load(SAMPLE)
    pf.holdings.extend(load_csv(FUNDS, AssetType.MUTUAL_FUND).holdings)
    disc = discover_disclosures(DISCLOSURE.parent)
    comps = fetch_compositions([f.name for f in pf.funds()], disclosures=disc)
    a = Analysis(pf, compositions=comps)
    # Orion disclosure resolved -> Gamma Tech gets fund exposure on top of direct
    gamma = next(e for e in a.lookthrough.exposures if e.name.startswith("Gamma"))
    assert gamma.via_funds and gamma.total > gamma.direct


def test_monitor_seeds_and_alerts(tmp_path=None):
    import tempfile
    from analyzer.alerts import AlertConfig, apply_cooldown, evaluate
    from analyzer.state import MonitorState, Snapshot
    d = tmp_path or Path(tempfile.mkdtemp())
    state = MonitorState.load(d / "state.json")

    pf, a, sugs = _analyse()
    cfg = AlertConfig(cooldown_hours=24)
    alerts = evaluate(a, sugs, state, cfg)
    # Beta NBFC concentration (28.6%) must raise a high concentration alert
    assert any(al.category == "concentration" and "Beta" in al.holding for al in alerts)

    fresh = apply_cooldown(alerts, state, cfg.cooldown_hours)
    assert fresh                                   # first run -> everything fresh
    # same run again -> cooldown suppresses the repeats
    again = apply_cooldown(evaluate(a, sugs, state, cfg), state, cfg.cooldown_hours)
    assert not again

    state.update_peaks(pf)
    state.last_snapshot = Snapshot.from_portfolio(pf)
    state.save()
    assert (d / "state.json").exists()


def test_monitor_detects_drop():
    import tempfile
    from analyzer.alerts import AlertConfig, evaluate
    from analyzer.state import MonitorState, Snapshot
    d = Path(tempfile.mkdtemp())
    state = MonitorState.load(d / "state.json")

    pf = load(SAMPLE)
    state.update_peaks(pf)
    state.last_snapshot = Snapshot.from_portfolio(pf)

    # crash one holding's price ~30%
    target = next(h for h in pf.equities() if h.name == "Gamma Tech")
    target.price = target.price * 0.7

    a = Analysis(pf)
    alerts = evaluate(a, [], state, AlertConfig())
    cats = {al.category for al in alerts if al.holding == "Gamma Tech"}
    assert "move" in cats or "drawdown" in cats


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
