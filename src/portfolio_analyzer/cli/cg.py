#!/usr/bin/env python3
"""Import a broker Capital-Gains statement and reconcile / recompute tax.

A CG report lists trades you already sold (realized gains), not current
holdings — so this is a **tax** tool, separate from the portfolio dashboard.
It parses the statement into rows, totals the gains per term, and recomputes
the tax those gains imply using fiscal-year-aware rates (rates follow each
trade's sell date).

    portfolio-cg statement.pdf --password ABCDE1234F
    portfolio-cg statement.txt --slab 0.30 --json

Tuned for the SBICAP Securities CG layout. Read-only. Not tax advice.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from portfolio_analyzer.cg import load_cg, recompute_tax


def _r(x) -> str:
    return f"₹{x:,.2f}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Capital-Gains statement importer / tax reconciler")
    ap.add_argument("statement", help="CG statement (.pdf / .txt / .csv-export)")
    ap.add_argument("--password", default="", help="PDF password (usually your PAN)")
    ap.add_argument("--slab", type=float, default=None, help="income-slab rate for debt gains")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    args = ap.parse_args(argv)

    res = load_cg(args.statement, args.password or None)
    if not res.records:
        print("No capital-gain rows found. If this is a PDF, install pypdf "
              "(pip install pypdf) or export the statement to text.", file=sys.stderr)
        return 1

    tax = recompute_tax(res, slab_rate=args.slab)

    if args.json:
        print(json.dumps({
            "records": [r.__dict__ for r in res.records],
            "by_term": res.by_term(),
            "tax": tax,
        }, indent=2, default=str, ensure_ascii=False))
        return 0

    print(f"Parsed {len(res.records)} realized-gain row(s):\n")
    print(f"  {'Term':5} {'Scrip':26} {'Buy':>11} {'Sell':>11} {'Gain':>13}")
    print("  " + "-" * 70)
    for r in res.records:
        print(f"  {r.term:5} {r.scrip[:26]:26} {str(r.buy_date):>11} "
              f"{str(r.sell_date):>11} {(r.gain or 0):>13,.2f}")
    print()
    for term, g in res.by_term().items():
        print(f"  {term} total gain: {_r(g)}")
    print()
    print("  Recomputed tax (fiscal-year-aware, incl. 4% cess):")
    for d in tax["by_regime"]:
        print(f"    [{d['regime']}] STCG {_r(d['stcg_gain'])} @ {d['stcg_rate']*100:.0f}% "
              f"→ {_r(d['stcg_tax'])};  LTCG {_r(d['ltcg_gain'])} @ {d['ltcg_rate']*100:.1f}% "
              f"(−{_r(d['ltcg_exemption'])} exempt) → {_r(d['ltcg_tax'])}")
    print(f"    TOTAL estimated tax: {_r(tax['total_tax'])}")
    print("\n  Not tax advice — verify against the statement and a tax professional.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
