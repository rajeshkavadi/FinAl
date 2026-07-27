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

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyzer.analytics import Analysis
from analyzer.loader import load
from analyzer.report import build_dashboard
from analyzer.rules import run_rules


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
    args = ap.parse_args(argv)

    pf = load(args.input)

    if args.live:
        from analyzer.prices import enrich_live
        status = enrich_live(pf)
        print(f"[live] equity updated={status['equity_updated']} "
              f"nav updated={status['nav_updated']} "
              f"errors={status['errors']}", file=sys.stderr)

    a = Analysis(pf)
    sugs = run_rules(a)

    html = build_dashboard(pf, a, sugs, title=args.title)
    Path(args.out).write_text(html, encoding="utf-8")

    print(f"Loaded {len(pf.equities())} equities, {len(pf.funds())} funds, "
          f"{len(pf.sips)} SIP plans.")
    print(f"Direct equity total: ₹{a.equity_total:,.0f} · "
          f"effective positions {a.equity_eff_positions:.1f} · "
          f"high-risk {a.high_risk_pct*100:.1f}%")
    print(f"{len(sugs)} suggestion(s):")
    for s in sugs:
        print(f"  [{s.severity.upper():4}] {s.title}")

    # Switch-tax estimate
    from analyzer.tax import TaxConfig
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
