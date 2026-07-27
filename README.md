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

Thresholds live in `analyzer/rules.py::DEFAULT_THRESHOLDS` and are easy to tune.

## Architecture
```
analyzer/
  models.py      Holding / Portfolio data model (stdlib only)
  loader.py      xlsx + CSV import (fuzzy column matching)
  prices.py      optional live providers: Yahoo (equity), AMFI (MF NAV)
  analytics.py   concentration, HHI, sector/theme/risk, overlap detection
  rules.py       transparent, threshold-driven suggestion engine
  report.py      self-contained HTML dashboard
run.py           CLI
tests/           smoke tests over the bundled sample
```

## Roadmap
- [x] Quantities → true current value, unrealised P/L (absolute & %), CAGR/XIRR
- [ ] MF↔equity look-through overlap (fund factsheet holdings vs direct stocks)
- [ ] STCG/LTCG tax estimate on each suggested switch
- [ ] Scheduled refresh + email/push alerts ("constantly monitor")
- [ ] Broker/CAS auto-sync (Zerodha Kite, MF Central) as an import source
```
```

## Tests
```bash
python tests/test_smoke.py     # or: python -m pytest
```
