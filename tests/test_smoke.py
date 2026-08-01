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


KITE = ROOT / "sample_data" / "example_kite_holdings.json"
BROKER = ROOT / "sample_data" / "example_broker_holdings.csv"
CAS = ROOT / "sample_data" / "example_cas.txt"


def test_parse_kite_holdings():
    import json as _json
    from analyzer.sync import parse_kite_holdings
    hs = parse_kite_holdings(_json.loads(KITE.read_text()))
    assert len(hs) == 3
    infy = next(h for h in hs if h.name == "INFY")
    assert infy.quantity == 50 and infy.avg_cost == 1400 and infy.price == 1600
    assert round(infy.invested) == 70000 and round(infy.current_value) == 80000
    # t1_quantity folds into quantity
    tata = next(h for h in hs if h.name == "TATASTEEL")
    assert tata.quantity == 210


def test_broker_csv_provider():
    from analyzer.sync import BrokerCSVProvider
    hs = BrokerCSVProvider(BROKER).holdings()
    assert len(hs) == 3
    rel = next(h for h in hs if h.name == "RELIANCE")
    assert rel.quantity == 40 and rel.avg_cost == 2400 and rel.price == 2650


def test_parse_cas_text():
    from analyzer.sync import load_cas
    from analyzer.models import AssetType
    hs = load_cas(CAS)
    assert len(hs) == 2
    ppfc = next(h for h in hs if "Parag Parikh" in h.name)
    assert ppfc.asset_type == AssetType.MUTUAL_FUND
    assert abs(ppfc.quantity - 1800.5) < 1e-6
    assert ppfc.price == 58.21
    assert round(ppfc.invested) == 100000            # cost value used
    assert ppfc.isin == "INF204K01234"
    assert round(ppfc.current_value) == 104807       # units * NAV (1800.5 * 58.21)


def test_sync_build_and_pl():
    from analyzer.sync import BrokerCSVProvider, build_portfolio
    from analyzer.analytics import Analysis
    pf = build_portfolio(broker=BrokerCSVProvider(BROKER), cas_path=CAS)
    assert len(pf.equities()) == 3 and len(pf.funds()) == 2
    a = Analysis(pf)
    # synced holdings carry qty+price -> P/L is fully computed
    assert pf.fully_valued
    assert a.pl.has_data and a.pl.total_pl is not None


def test_sync_to_csv_roundtrips_into_loader():
    import tempfile
    from analyzer.sync import BrokerCSVProvider, build_portfolio, to_csv
    d = Path(tempfile.mkdtemp())
    pf = build_portfolio(broker=BrokerCSVProvider(BROKER))
    to_csv(pf, d / "equity.csv", d / "funds.csv")
    reloaded = load(d / "equity.csv")
    assert len(reloaded.equities()) == 3
    rel = next(h for h in reloaded.equities() if h.name == "RELIANCE")
    assert rel.quantity == 40 and rel.valued_on_market


def test_multipart_roundtrip():
    import webapp
    boundary = b"----webkitTESTBOUNDARY"
    def part(name, filename, content):
        head = f'Content-Disposition: form-data; name="{name}"'
        if filename:
            head += f'; filename="{filename}"'
        return (b"--" + boundary + b"\r\n" + head.encode() + b"\r\n\r\n"
                + content + b"\r\n")
    body = (part("portfolio", "p.csv", b"Stock,Price,Invested\nX,10,1000\n")
            + part("cas_password", None, b"ABCDE1234F")
            + b"--" + boundary + b"--\r\n")
    parsed = webapp.parse_multipart(body, boundary)
    assert set(parsed) == {"portfolio", "cas_password"}
    assert parsed["portfolio"]["filename"] == "p.csv"
    assert parsed["portfolio"]["content"] == b"Stock,Price,Invested\nX,10,1000\n"
    assert parsed["cas_password"]["content"] == b"ABCDE1234F"


def test_webapp_assemble_and_analyze():
    import webapp
    parts = {
        "broker": {"filename": "b.csv", "content": BROKER.read_bytes()},
        "cas": {"filename": "cas.txt", "content": CAS.read_bytes()},
        "fund_holdings": {"filename": "fh.csv", "content": FUND_HOLDINGS.read_bytes()},
    }
    htmlout = webapp.analyze_parts(parts)
    assert "<!doctype html>" in htmlout.lower()
    assert "Profit &amp; loss" in htmlout or "Profit & loss" in htmlout
    assert "Restructuring suggestions" in htmlout


def test_webapp_http_end_to_end():
    import http.client
    import threading
    from http.server import ThreadingHTTPServer
    import webapp
    srv = ThreadingHTTPServer(("127.0.0.1", 0), webapp.Handler)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.request("GET", "/")
        r = conn.getresponse()
        assert r.status == 200
        assert b"Portfolio Analyzer" in r.read()
        conn.request("GET", "/sample")          # runs analysis on bundled data
        r = conn.getresponse()
        assert r.status == 200
        assert b"Restructuring suggestions" in r.read()
    finally:
        srv.shutdown()


if __name__ == "__main__":
    passed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
            passed += 1
    print(f"\n{passed} tests passed")
