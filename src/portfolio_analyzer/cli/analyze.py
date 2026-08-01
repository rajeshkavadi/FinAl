#!/usr/bin/env python3
"""CLI entry point for the portfolio analyzer.

Usage:
    python run.py path/to/portfolio.xlsx [--out dashboard.html] [--live] [--json]

  --live   attempt to fetch live equity prices (Yahoo) and MF NAVs (AMFI).
           Requires internet; silently falls back to imported prices.
  --json   also print the analysis + suggestions as JSON to stdout.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


from portfolio_analyzer.analytics import Analysis
from portfolio_analyzer.loader import load
from portfolio_analyzer.report import build_dashboard
from portfolio_analyzer.rules import run_rules


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="MF + equity portfolio analyzer")
    ap.add_argument("input", help="portfolio .xlsx or .csv")
    ap.add_argument("--out", default="dashboard.html", help="output HTML path")
    ap.add_argument("--live", action="store_true", help="fetch live prices/NAVs")
    ap.add_argument("--json", action="store_true", help="also emit JSON to stdout")
    ap.add_argument("--title", default="Portfolio Analysis")
    ap.add_argument("--tax-on", default="",
                    help="comma-separated holding names to estimate switch tax for "
                         "(default: the suggestion-flagged exit candidates)")
    ap.add_argument("--slab", type=float, default=None,
                    help="your income-slab rate as a fraction (e.g. 0.30) for "
                         "debt/non-equity gains")
    ap.add_argument("--funds", default="",
                    help="CSV of current MF holdings (Fund, Units, NAV, Invested) "
                         "to add fund values for look-through")
    ap.add_argument("--fund-holdings", dest="fund_holdings", default="",
                    help="CSV/JSON of fund compositions (Fund, Stock, Weight[, Sector]) "
                         "for MF<->equity look-through")
    ap.add_argument("--disclosures", default="",
                    help="folder of AMC monthly portfolio-disclosure files to parse "
                         "into fund compositions")
    ap.add_argument("--compositions-url", dest="comp_url", default="",
                    help="URL template ({fund}) to fetch a disclosure per fund")
    ap.add_argument("--comp-cache", default="",
                    help="path to cache fetched compositions (freshness ~35d)")
    args = ap.parse_args(argv)

    pf = load(args.input)

    if args.funds:
        from portfolio_analyzer.models import AssetType
        from portfolio_analyzer.loader import load_csv
        pf.holdings.extend(load_csv(args.funds, AssetType.MUTUAL_FUND).holdings)

    compositions = None
    if args.fund_holdings:
        from portfolio_analyzer.lookthrough import load_compositions
        compositions = load_compositions(args.fund_holdings)

    if args.disclosures or args.comp_url or args.comp_cache:
        from portfolio_analyzer.compositions_fetch import (
            CompositionCache, HttpCompositionProvider, discover_disclosures,
            fetch_compositions)
        fund_names = [f.name for f in pf.funds()]
        disc = discover_disclosures(args.disclosures) if args.disclosures else {}
        # a disclosure file may not match a holding name exactly; also expose
        # disclosure funds even when no matching MF holding is loaded
        fund_names = list(dict.fromkeys(fund_names + list(disc.keys())))
        provider = HttpCompositionProvider(args.comp_url) if args.comp_url else None
        cache = CompositionCache(args.comp_cache) if args.comp_cache else None
        fetched = fetch_compositions(fund_names, disclosures=disc,
                                     provider=provider, cache=cache)
        compositions = {**(compositions or {}), **fetched}
        print(f"[compositions] resolved {len(fetched)} fund(s) "
              f"(disclosures={len(disc)}, url={'yes' if provider else 'no'})",
              file=sys.stderr)

    if args.live:
        from portfolio_analyzer.prices import enrich_live
        status = enrich_live(pf)
        print(f"[live] equity updated={status['equity_updated']} "
              f"nav updated={status['nav_updated']} "
              f"errors={status['errors']}", file=sys.stderr)

    a = Analysis(pf, compositions=compositions)
    sugs = run_rules(a)

    html = build_dashboard(pf, a, sugs, title=args.title)
    Path(args.out).write_text(html, encoding="utf-8")

    print(f"Loaded {len(pf.equities())} equities, {len(pf.funds())} funds, "
          f"{len(pf.sips)} SIP plans.")
    print(f"Direct equity total: ₹{a.equity_total:,.0f} · "
          f"effective positions {a.equity_eff_positions:.1f} · "
          f"high-risk {a.high_risk_pct*100:.1f}%")
    if a.lookthrough:
        lt = a.lookthrough
        print(f"Look-through: base ₹{lt.total_base:,.0f}, "
              f"{len(lt.direct_and_fund_overlaps())} direct+fund overlap(s), "
              f"{len(lt.multi_fund_overlaps())} multi-fund overlap(s)")

    print(f"{len(sugs)} suggestion(s):")
    for s in sugs:
        print(f"  [{s.severity.upper():4}] {s.title}")

    # Switch-tax estimate
    from portfolio_analyzer.tax import TaxConfig
    names = ([n.strip() for n in args.tax_on.split(",") if n.strip()]
             or a.sell_candidates(sugs))
    if names:
        rep = a.switch_tax(names, config=TaxConfig(slab_rate=args.slab))
        if rep.lines:
            print(f"Switch-tax estimate on {len(rep.lines)} position(s): "
                  f"total tax ₹{rep.total_tax:,.0f} on ₹{rep.gross_proceeds:,.0f} "
                  f"proceeds (drag {rep.tax_drag_pct*100:.1f}%)")

    print(f"Dashboard written to {args.out}")

    if args.json:
        print(json.dumps({
            "equity_total": a.equity_total,
            "effective_positions": a.equity_eff_positions,
            "hhi": a.equity_hhi,
            "high_risk_pct": a.high_risk_pct,
            "by_stock": [c.__dict__ for c in a.by_stock],
            "by_theme": [c.__dict__ for c in a.by_theme],
            "suggestions": [s.to_dict() for s in sugs],
        }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
