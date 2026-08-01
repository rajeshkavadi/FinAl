# Portfolio Analyzer (MF + Direct Equity)

A local, privacy-first tool that reads your holdings, measures **concentration,
sector/theme exposure, risk, and overlap**, and generates transparent,
rule-based **restructuring suggestions** — for *both* mutual funds and direct
equity. Think Dezerv/PowerUp-style analysis, but covering shares as well as MFs,
running entirely on your own machine.

> **Not investment advice.** This is an organisational/analytical tool. It never
> places trades. Verify all prices and quantities against your broker/demat
> statements and consult a SEBI-registered adviser before acting.

## Why
Received advice often rotates individual names but leaves the real risks —
single-stock concentration, sector over-weight, a high-risk-heavy book — in
place. This engine measures those explicitly and flags them, whatever the
underlying names are.

## Install
```bash
cd portfolio_app
pip install -r requirements.txt   # only needed for .xlsx input; CSV needs nothing
```

## Web UI
Prefer a browser to the CLI? Run the local web app (stdlib only, no Flask):
```bash
python webapp.py            # then open http://127.0.0.1:8765
```
Upload any combination of a portfolio file, broker holdings CSV, CAS statement,
MF-holdings CSV and fund-compositions file (all fields optional) and it returns
the full dashboard. There's a **“Try with sample data”** button too. It binds to
`127.0.0.1` on purpose — your files are processed in memory on your machine and
never sent anywhere.

## Use
```bash
# From your tracker spreadsheet
python run.py path/to/portfolio_tracker.xlsx --out dashboard.html

# From a broker CSV export
python run.py sample_data/example_portfolio.csv --out dashboard.html

# Pull live equity prices (Yahoo) + MF NAVs (AMFI) when online
python run.py portfolio.xlsx --live

# Also print machine-readable JSON
python run.py portfolio.xlsx --json
```
Open the generated `dashboard.html` in any browser.

## Input format
The loader matches columns fuzzily (case-insensitive), so most broker exports
work. Recognised sheets/columns:

- **SIPs / MF sheet**: `Fund` · `Category` · `Monthly SIP` · `Verdict` · `Notes`
- **Direct Equity sheet**: `Stock` · `Sector/Theme` · `Price` · `Invested` ·
  (optional) `Quantity` · `Avg Cost` · `Buy Date` · `Risk Flag` · `Notes`

Valuation upgrades automatically with the data you provide:

| You supply | You get |
|------------|---------|
| Invested + Price only | concentration / sector / risk / overlap (cost basis) |
| **+ Quantity** | current market value, **unrealised P/L** (₹ and %), winners/losers |
| **+ Buy Date** | per-holding **CAGR** and portfolio **XIRR** (money-weighted) |

Given any two of *Invested / Quantity / Avg Cost*, the third is derived. SIPs
with a `SIP Start` date and current corpus contribute synthesized monthly
cashflows to the XIRR.

## What it checks (rules)
| Rule | Fires when |
|------|-----------|
| Single-stock concentration | any stock > 15% (warn) / 20% (high) of equity |
| Sector/theme concentration | any theme (e.g. all IT flavours) > 25% |
| High-risk book | High + Very-High flagged names > 40% |
| Diversification breadth | effective positions (1/HHI) below target |
| Averaging into speculative | same name held across multiple tranches |
| MF category overlap | 2+ funds in the same category |
| Micro positions | several sub-1.5% positions adding tracking noise |
| Hidden concentration | a stock's look-through weight (direct + funds) breaches the single-stock guideline |
| Direct∩fund overlap | a stock is held both directly and inside your funds |

Thresholds live in `analyzer/rules.py::DEFAULT_THRESHOLDS` and are easy to tune.
The last two require look-through data (see below).

## Tax-on-switch (STCG/LTCG)
Before you act on a suggestion, the app estimates the capital-gains tax of
actually exiting the flagged positions (current Indian rules, post-Jul-2024):

- Listed equity / equity MF: **STCG 20%** (≤12m), **LTCG 12.5%** (>12m) on gains
  above the **₹1.25L** annual exemption; **debt MF** at slab (`--slab`)
- Short/long-term **losses are netted within their term**; 4% cess added;
  surcharge excluded (income-dependent)
- Reports total tax, **net proceeds**, **tax drag %**, and the **break-even
  out-performance** the replacement must deliver just to recover the tax

```bash
python run.py portfolio.xlsx --tax-on "Jyoti Structures,Mastek" --slab 0.30
```
Needs quantity, price and buy date on the holdings to be exact; without a buy
date a position is treated as short-term (conservative). **Estimate only — not
tax advice.**

## Auto-sync (broker + CAS) — no manual entry
`sync.py` pulls **real holdings with quantities** so P/L, XIRR and tax work on
your actual portfolio. **Read-only — it never trades.**

- **Direct equity** — Zerodha **Kite Connect** (`/portfolio/holdings`, needs an
  API key + a daily access token) or **any broker's holdings CSV export**
- **Mutual funds** — the **CAS** (Consolidated Account Statement) from
  CAMS/KFintech: a `.pdf` (parsed via `pdfplumber`/`pypdf`, password = your PAN)
  or a text/csv export

```bash
# broker CSV + CAS -> normalized CSVs + a dashboard
python sync.py --broker-csv console_holdings.csv \
  --cas cas_statement.pdf --cas-password ABCDE1234F \
  --out-equity equity.csv --out-funds funds.csv --dashboard dashboard.html

# Zerodha Kite
python sync.py --kite-key $KITE_KEY --kite-token $KITE_TOKEN --out-equity equity.csv
```
The written `equity.csv` / `funds.csv` feed `run.py` and `monitor.py` directly,
so the whole loop is *sync → analyse → monitor*. The response/CSV/CAS-text
parsers are pure and unit-tested; the Kite network call and PDF extraction are
isolated and best-effort (PDF parsing needs `pip install pdfplumber`).

## Look-through (MF ↔ equity)
Your funds hold stocks too. If you also own those stocks directly, your *true*
exposure is higher than either view shows. Supply fund values and each fund's
constituents and the app combines them into one exposure map:

```bash
python run.py portfolio.xlsx \
  --funds sample_data/example_funds.csv \
  --fund-holdings sample_data/example_fund_holdings.csv
```
- `--funds` — current MF holdings with value: `Fund, Units, NAV, Invested`
- `--fund-holdings` — compositions from factsheets/disclosures:
  `Fund, Stock, Weight[, Sector]` (weight as % or fraction); JSON also accepted

It reports each stock's **true %** vs **direct %**, which funds contribute, and a
`direct + fund` / `multi-fund` overlap flag, and raises a **hidden-concentration**
suggestion when a name's combined weight breaches the single-stock guideline
even if the direct leg alone doesn't. Names are matched fuzzily (Ltd/Limited/
India suffixes normalised); un-named/cash holdings count toward the base but
aren't attributed to a stock.

### Getting compositions automatically
Instead of hand-entering `--fund-holdings`, point the app at the **monthly
portfolio disclosure** every AMC publishes (SEBI-mandated). It parses the
spreadsheet, keeps equity constituents (drops debt / TREPS / cash / totals via
name + rating heuristics), scales `% to Net Assets` to weights, and caches the
result (holdings change ~monthly, so refetching daily is pointless):

```bash
# parse a folder of downloaded disclosure files (xlsx/csv), cache the result
python run.py portfolio.xlsx --funds funds.csv \
  --disclosures ./disclosures --comp-cache ~/.portfolio_monitor/comp_cache.json

# later runs need only the cache (served if <35 days old)
python run.py portfolio.xlsx --funds funds.csv \
  --comp-cache ~/.portfolio_monitor/comp_cache.json

# or fetch each fund's disclosure from a URL template
python run.py portfolio.xlsx --funds funds.csv \
  --compositions-url "https://data.example.com/{fund}.xlsx" \
  --comp-cache ~/.portfolio_monitor/comp_cache.json
```
Resolution order per fund: **cache → local disclosure file → HTTP provider**.
The same flags work on `monitor.py`. There is no single official holdings API in
India, so this targets the disclosure files (and any URL you have rights to);
scraping third-party sites is intentionally not built in.

## Monitoring ("constantly monitor")
`monitor.py` snapshots the portfolio each run, diffs it against the last
snapshot, evaluates alert rules, and dispatches **new** alerts (per-alert
cooldown prevents spam) to pluggable notifiers.

**Alerts:** single-stock / sector concentration breach (uses look-through when
available), position down since last check, portfolio down since last check,
drawdown from peak, single-day price move, **LTCG-eligibility crossing** (sell
after this date for 12.5% vs 20%), and any live high-severity suggestion.

**Notifiers** (stdlib, chosen via env): console (always), markdown log file,
SMTP email, and HTTP webhook (Slack/Discord/Zapier).

```bash
# one shot (cron/systemd friendly)
python monitor.py portfolio.xlsx --live

# loop every 30 min, log to a file
PORTFOLIO_ALERT_LOG=alerts.md python monitor.py portfolio.xlsx --live --watch 30

# weekday 9:30 IST via cron, Slack + email
# 30 9 * * 1-5  PORTFOLIO_WEBHOOK_URL=... SMTP_HOST=... ALERT_TO=you@x.com \
#   python /path/monitor.py /path/portfolio.xlsx --live
```
Env: `PORTFOLIO_ALERT_LOG`, `PORTFOLIO_WEBHOOK_URL`, `SMTP_HOST` / `SMTP_PORT` /
`SMTP_USER` / `SMTP_PASS` / `ALERT_FROM` / `ALERT_TO`. State (snapshots, peaks,
cooldowns) persists in `~/.portfolio_monitor/state.json` (`--state` to override,
`--cooldown` hours to tune re-fire frequency). For true always-on monitoring,
drive the one-shot form from cron / systemd timer / Windows Task Scheduler.

## Architecture
```
analyzer/
  models.py      Holding / Portfolio data model (stdlib only)
  loader.py      xlsx + CSV import (fuzzy column matching)
  returns.py     XIRR / CAGR / SIP cashflow synthesis
  tax.py         STCG/LTCG switch-tax estimator (Sec 111A / 112A)
  lookthrough.py MF constituents -> combined true exposure + overlaps
  compositions_fetch.py  parse AMC monthly disclosures + cache + HTTP provider
  sync.py        broker (Kite / CSV) + CAS auto-sync -> holdings
  prices.py      optional live providers: Yahoo (equity), AMFI (MF NAV)
  analytics.py   concentration, HHI, sector/theme/risk, P/L, look-through
  rules.py       transparent, threshold-driven suggestion engine
  state.py       snapshots, peaks, and alert cooldowns (JSON)
  alerts.py      alert rules (level + change triggers)
  notify.py      notifiers: console / file / email / webhook
  report.py      self-contained HTML dashboard
run.py           analysis CLI (one-shot dashboard)
monitor.py       monitoring CLI (snapshot + diff + alert + notify; --watch)
sync.py          auto-sync CLI (broker + CAS -> normalized CSVs)
webapp.py        local web UI (stdlib http.server; upload -> dashboard)
tests/           smoke tests over the bundled sample
```

## Roadmap
- [x] Quantities → true current value, unrealised P/L (absolute & %), CAGR/XIRR
- [x] STCG/LTCG tax estimate on each suggested switch (Sec 111A / 112A, cess,
      ₹1.25L LTCG exemption, loss set-off within term)
- [x] MF↔equity look-through overlap (fund constituents vs direct stocks →
      true per-stock exposure + hidden-concentration flags)
- [x] Scheduled refresh + email/webhook alerts ("constantly monitor")
- [x] Live fetcher for fund compositions (parse AMC monthly disclosures; cache)
- [x] Broker/CAS auto-sync (Zerodha Kite / broker CSV + CAS statement)
```
```

## Tests
```bash
python tests/test_smoke.py     # or: python -m pytest
```
