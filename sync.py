#!/usr/bin/env python3
"""Auto-sync CLI: pull holdings from a broker and/or CAS into normalized CSVs.

Read-only. Never places trades.

Examples:
    # Zerodha Kite (needs an active access token from Kite's login flow)
    python sync.py --kite-key $KITE_KEY --kite-token $KITE_TOKEN \
        --out-equity equity.csv

    # A broker's holdings CSV export + a CAS PDF for mutual funds
    python sync.py --broker-csv console_holdings.csv \
        --cas cas_statement.pdf --cas-password MYPAN1234A \
        --out-equity equity.csv --out-funds funds.csv --dashboard dashboard.html

The written CSVs feed run.py / monitor.py directly, so a sync then an analysis
is the whole loop:
    python sync.py --broker-csv h.csv --out-equity equity.csv
    python run.py equity.csv --out dashboard.html
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyzer.sync import (BrokerCSVProvider, ZerodhaKiteProvider,
                           build_portfolio, to_csv)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Broker/CAS auto-sync (read-only)")
    ap.add_argument("--kite-key", default=os.environ.get("KITE_KEY", ""))
    ap.add_argument("--kite-token", default=os.environ.get("KITE_TOKEN", ""))
    ap.add_argument("--broker-csv", default="", help="broker holdings CSV export")
    ap.add_argument("--cas", default="", help="CAS file (.pdf/.txt/.csv) for MF holdings")
    ap.add_argument("--cas-password", default=os.environ.get("CAS_PASSWORD", ""))
    ap.add_argument("--out-equity", default="equity.csv")
    ap.add_argument("--out-funds", default="funds.csv")
    ap.add_argument("--dashboard", default="", help="also render a dashboard here")
    args = ap.parse_args(argv)

    broker = None
    if args.broker_csv:
        broker = BrokerCSVProvider(args.broker_csv)
    elif args.kite_key and args.kite_token:
        broker = ZerodhaKiteProvider(args.kite_key, args.kite_token)

    if not broker and not args.cas:
        ap.error("nothing to sync — give --broker-csv/--kite-* and/or --cas")

    pf = build_portfolio(broker=broker, cas_path=args.cas or None,
                         cas_password=args.cas_password or None)
    eq, funds = pf.equities(), pf.funds()
    print(f"Synced {len(eq)} equity holding(s), {len(funds)} fund holding(s).")
    if not eq and not funds:
        print("No holdings found — check credentials / file format.", file=sys.stderr)
        return 1

    to_csv(pf, args.out_equity, args.out_funds)
    if eq:
        print(f"  wrote {args.out_equity}")
    if funds:
        print(f"  wrote {args.out_funds}")

    if args.dashboard:
        from analyzer.analytics import Analysis
        from analyzer.report import build_dashboard
        from analyzer.rules import run_rules
        a = Analysis(pf)
        sugs = run_rules(a)
        Path(args.dashboard).write_text(
            build_dashboard(pf, a, sugs, title="Synced Portfolio"), encoding="utf-8")
        print(f"  wrote {args.dashboard}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
