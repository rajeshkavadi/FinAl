"""Load holdings from the portfolio tracker xlsx or generic CSV files.

The xlsx layout supported here matches the user's tracker:
  - a "SIPs" sheet: Fund, Category, Monthly SIP, Annual SIP, Verdict, Notes
  - a "Direct Equity*" sheet: Stock, Sector/Theme, Price, Invested, %, Risk, Notes

Column matching is fuzzy (case-insensitive substring) so slightly different
broker/export headers still work. A generic CSV loader is also provided.
"""
from __future__ import annotations

import csv
import datetime as _dt
from pathlib import Path
from typing import Optional

from .models import AssetType, Holding, Portfolio


def _date(v) -> Optional[_dt.date]:
    """Parse a date cell across common formats; tolerate blanks/datetimes."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    if isinstance(v, _dt.datetime):
        return v.date()
    if isinstance(v, _dt.date):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%m/%d/%Y",
                "%d-%b-%Y", "%d %b %Y", "%Y/%m/%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _num(v) -> Optional[float]:
    """Coerce a cell to float, tolerating ₹, commas, %, blanks, text."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("₹", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _find(headers: list[str], *needles: str) -> Optional[int]:
    """Return the index of the first header containing any needle (ci)."""
    low = [(h or "").strip().lower() for h in headers]
    for needle in needles:
        for i, h in enumerate(low):
            if needle in h:
                return i
    return None


def _is_total_row(cells: list) -> bool:
    first = str(cells[0] or "").strip().lower()
    return first.startswith("total") or first.startswith("note")


# --------------------------------------------------------------------------
# xlsx
# --------------------------------------------------------------------------
def load_xlsx(path: str | Path) -> Portfolio:
    import openpyxl  # imported lazily so CSV-only users need no dependency

    wb = openpyxl.load_workbook(path, data_only=True)
    pf = Portfolio(meta={"source": str(path)})

    for ws in wb.worksheets:
        title = ws.title.lower()
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c is not None for c in r)]
        if not rows:
            continue

        if "sip" in title:
            pf.sips.extend(_parse_sips(rows))
        elif "replace" in title or "suggest" in title:
            pf.meta.setdefault("replacements", []).extend(_parse_replacements(rows))
        elif "equity" in title or "stock" in title or "direct" in title:
            pf.holdings.extend(_parse_equity(rows))
        elif "fund" in title and "sip" not in title:
            # a current-MF-holdings sheet, if the user adds one later
            pf.holdings.extend(_parse_funds(rows))
    return pf


def _parse_replacements(rows: list[list]) -> list[dict]:
    hdr = rows[0]
    i_cur = _find(hdr, "current", "holding", "from")
    i_val = _find(hdr, "value", "amount")
    i_rep = _find(hdr, "replacement", "suggested", "to")
    i_rat = _find(hdr, "rationale", "reason", "note")
    out: list[dict] = []
    for cells in rows[1:]:
        if _is_total_row(cells) or i_cur is None or not cells[i_cur]:
            continue
        out.append({
            "current": str(cells[i_cur]).strip(),
            "value": _num(cells[i_val]) if i_val is not None else None,
            "replacement": str(cells[i_rep]).strip() if i_rep is not None and cells[i_rep] else "",
            "rationale": str(cells[i_rat]).strip() if i_rat is not None and cells[i_rat] else "",
        })
    return out


def _parse_sips(rows: list[list]) -> list[Holding]:
    hdr = rows[0]
    i_fund = _find(hdr, "fund", "scheme", "name")
    i_cat = _find(hdr, "category")
    i_month = _find(hdr, "monthly")
    i_verdict = _find(hdr, "verdict", "action")
    i_notes = _find(hdr, "note")
    out: list[Holding] = []
    for cells in rows[1:]:
        if _is_total_row(cells) or not cells[i_fund or 0]:
            continue
        monthly = _num(cells[i_month]) if i_month is not None else None
        out.append(Holding(
            name=str(cells[i_fund]).strip(),
            asset_type=AssetType.SIP,
            invested=(monthly or 0) * 12,   # annualised flow, informational
            category=str(cells[i_cat]).strip() if i_cat is not None and cells[i_cat] else "",
            verdict=str(cells[i_verdict]).strip() if i_verdict is not None and cells[i_verdict] else None,
            notes=str(cells[i_notes]).strip() if i_notes is not None and cells[i_notes] else "",
            monthly_sip=monthly,
        ))
    return out


def _parse_equity(rows: list[list]) -> list[Holding]:
    hdr = rows[0]
    i_name = _find(hdr, "stock", "name", "scrip")
    i_sec = _find(hdr, "sector", "theme")
    i_price = _find(hdr, "price", "ltp")
    i_inv = _find(hdr, "invested", "value", "cost", "amount")
    i_qty = _find(hdr, "qty", "quantity", "units", "shares")
    i_avg = _find(hdr, "avg cost", "avg price", "buy price", "avg buy", "cost/unit")
    i_bdate = _find(hdr, "buy date", "purchase date", "acquired", "date")
    i_risk = _find(hdr, "risk")
    i_notes = _find(hdr, "note")
    out: list[Holding] = []
    for cells in rows[1:]:
        if _is_total_row(cells) or i_name is None or not cells[i_name]:
            continue
        out.append(Holding(
            name=str(cells[i_name]).strip(),
            asset_type=AssetType.EQUITY,
            invested=_num(cells[i_inv]) or 0.0 if i_inv is not None else 0.0,
            sector=str(cells[i_sec]).strip() if i_sec is not None and cells[i_sec] else "Uncategorised",
            price=_num(cells[i_price]) if i_price is not None else None,
            quantity=_num(cells[i_qty]) if i_qty is not None else None,
            avg_cost=_num(cells[i_avg]) if i_avg is not None else None,
            buy_date=_date(cells[i_bdate]) if i_bdate is not None else None,
            risk_flag=str(cells[i_risk]).strip() if i_risk is not None and cells[i_risk] else None,
            notes=str(cells[i_notes]).strip() if i_notes is not None and cells[i_notes] else "",
        ))
    return out


def _parse_funds(rows: list[list]) -> list[Holding]:
    hdr = rows[0]
    i_name = _find(hdr, "fund", "scheme", "name")
    i_cat = _find(hdr, "category")
    i_inv = _find(hdr, "invested", "value", "cost", "amount", "corpus")
    i_units = _find(hdr, "units", "quantity", "qty")
    i_nav = _find(hdr, "nav", "price")
    i_isin = _find(hdr, "isin")
    i_risk = _find(hdr, "risk")
    i_notes = _find(hdr, "note")
    out: list[Holding] = []
    for cells in rows[1:]:
        if _is_total_row(cells) or i_name is None or not cells[i_name]:
            continue
        out.append(Holding(
            name=str(cells[i_name]).strip(),
            asset_type=AssetType.MUTUAL_FUND,
            invested=_num(cells[i_inv]) or 0.0 if i_inv is not None else 0.0,
            category=str(cells[i_cat]).strip() if i_cat is not None and cells[i_cat] else "",
            sector="Mutual Fund",
            quantity=_num(cells[i_units]) if i_units is not None else None,
            price=_num(cells[i_nav]) if i_nav is not None else None,
            isin=str(cells[i_isin]).strip() if i_isin is not None and cells[i_isin] else None,
            risk_flag=str(cells[i_risk]).strip() if i_risk is not None and cells[i_risk] else None,
            notes=str(cells[i_notes]).strip() if i_notes is not None and cells[i_notes] else "",
        ))
    return out


# --------------------------------------------------------------------------
# CSV (generic broker export)
# --------------------------------------------------------------------------
def load_csv(path: str | Path, asset_type: AssetType = AssetType.EQUITY) -> Portfolio:
    pf = Portfolio(meta={"source": str(path)})
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.reader(fh)
        rows = [r for r in reader if any(c.strip() for c in r)]
    if not rows:
        return pf
    if asset_type == AssetType.SIP:
        pf.sips.extend(_parse_sips(rows))
    elif asset_type == AssetType.MUTUAL_FUND:
        pf.holdings.extend(_parse_funds(rows))
    else:
        pf.holdings.extend(_parse_equity(rows))
    return pf


def load(path: str | Path) -> Portfolio:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        return load_xlsx(p)
    return load_csv(p)
