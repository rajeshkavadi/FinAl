#!/usr/bin/env python3
"""Portfolio monitor — the "constantly monitor" layer.

Runs the analysis, diffs against the last snapshot, evaluates alert rules, and
dispatches new alerts (respecting a per-alert cooldown) to the configured
notifiers. Use ``--watch N`` to loop every N minutes, or run once from cron /
systemd / Task Scheduler for true always-on monitoring.

Examples:
    # one shot, print to console, pull live prices
    python monitor.py portfolio.xlsx --live

    # loop every 30 min with fund look-through, log to a file
    PORTFOLIO_ALERT_LOG=alerts.md python monitor.py portfolio.xlsx \
        --funds funds.csv --fund-holdings holdings.csv --live --watch 30

    # cron (every weekday 09:30 IST): notify via Slack webhook + email
    # 30 9 * * 1-5  PORTFOLIO_WEBHOOK_URL=... SMTP_HOST=... ALERT_TO=... \
    #   python /path/monitor.py /path/portfolio.xlsx --live
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from analyzer.alerts import AlertConfig, apply_cooldown, evaluate
from analyzer.analytics import Analysis
from analyzer.loader import load, load_csv
from analyzer.models import AssetType
from analyzer.notify import build_from_env, dispatch
from analyzer.rules import run_rules
from analyzer.state import MonitorState, Snapshot


def run_once(args, notifiers, state: MonitorState) -> int:
    pf = load(args.input)
    if args.funds:
        pf.holdings.extend(load_csv(args.funds, AssetType.MUTUAL_FUND).holdings)

    if args.live:
        from analyzer.prices import enrich_live
        enrich_live(pf)

    compositions = None
    if args.fund_holdings:
        from analyzer.lookthrough import load_compositions
        compositions = load_compositions(args.fund_holdings)

    if args.disclosures or args.comp_url or args.comp_cache:
        from analyzer.compositions_fetch import (
            CompositionCache, HttpCompositionProvider, discover_disclosures,
            fetch_compositions)
        disc = discover_disclosures(args.disclosures) if args.disclosures else {}
        fund_names = list(dict.fromkeys(
            [f.name for f in pf.funds()] + list(disc.keys())))
        provider = HttpCompositionProvider(args.comp_url) if args.comp_url else None
        cache = CompositionCache(args.comp_cache) if args.comp_cache else None
        fetched = fetch_compositions(fund_names, disclosures=disc,
                                     provider=provider, cache=cache)
        compositions = {**(compositions or {}), **fetched}

    a = Analysis(pf, compositions=compositions)
    sugs = run_rules(a)

    cfg = AlertConfig(cooldown_hours=args.cooldown)
    alerts = evaluate(a, sugs, state, cfg)
    fresh = apply_cooldown(alerts, state, cfg.cooldown_hours)

    dispatch(fresh, notifiers)

    # advance state
    state.update_peaks(pf)
    state.last_snapshot = Snapshot.from_portfolio(pf)
    state.save()
    return len(fresh)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Portfolio monitor / alerting")
    ap.add_argument("input", help="portfolio .xlsx or .csv")
    ap.add_argument("--funds", default="", help="MF holdings CSV (for look-through)")
    ap.add_argument("--fund-holdings", dest="fund_holdings", default="",
                    help="fund compositions CSV/JSON (for look-through)")
    ap.add_argument("--disclosures", default="",
                    help="folder of AMC portfolio-disclosure files to parse")
    ap.add_argument("--compositions-url", dest="comp_url", default="",
                    help="URL template ({fund}) to fetch a disclosure per fund")
    ap.add_argument("--comp-cache", default="",
                    help="path to cache fetched compositions")
    ap.add_argument("--live", action="store_true", help="fetch live prices/NAVs")
    ap.add_argument("--state", default=None, help="state file (default ~/.portfolio_monitor/state.json)")
    ap.add_argument("--cooldown", type=float, default=24.0,
                    help="hours before the same alert can re-fire (default 24)")
    ap.add_argument("--watch", type=float, default=0.0,
                    help="loop every N minutes instead of running once")
    args = ap.parse_args(argv)

    state_path = args.state or MonitorState.load().path
    notifiers = build_from_env()

    if args.watch <= 0:
        state = MonitorState.load(state_path) if args.state else MonitorState.load()
        run_once(args, notifiers, state)
        return 0

    interval = args.watch * 60.0
    print(f"[monitor] watching '{args.input}' every {args.watch:g} min "
          f"(cooldown {args.cooldown:g}h). Ctrl-C to stop.")
    try:
        while True:
            state = MonitorState.load(state_path) if args.state else MonitorState.load()
            n = run_once(args, notifiers, state)
            print(f"[monitor] cycle done, {n} new alert(s); sleeping {args.watch:g} min")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\n[monitor] stopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
