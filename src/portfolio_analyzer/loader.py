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
import re
from pathlib import Path
from typing import Optional

from .models import AssetType, Holding, Portfolio

_SIP_RE = re.compile(r"sip\s*of\s*(?:rs\.?|inr|₹)?\s*([\d,]+)", re.I)
_PM_RE = re.compile(r"(?:rs\.?|inr|₹)?\s*([\d,]+)\s*(?:p\.?\s*m\b|per\s*month|/\s*month|monthly)", re.I)


def _sip_amount(cells: list) -> Optional[float]:
    """Pull a monthly SIP figure out of free text like 'SIP of 10000 p.m.'."""
    for c in cells:
        if isinstance(c, str):
            m = _SIP_RE.search(c) or _PM_RE.search(c)
            if m:
                return _num(m.group(1))
    return None


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

    sheets: list[tuple[str, list]] = []
    for ws in wb.worksheets:
        rows = [list(r) for r in ws.iter_rows(values_only=True)]
        rows = [r for r in rows if any(c is not None for c in r)]
        if rows:
            sheets.append((ws.title.lower(), rows))

    # 1) named-sheet dispatch (original tracker layout)
    for title, rows in sheets:
        if "sip" in title:
            pf.sips.extend(_parse_sips(rows))
        elif "replace" in title or "suggest" in title:
            pf.meta.setdefault("replacements", []).extend(_parse_replacements(rows))
        elif "equity" in title or "stock" in title or "direct" in title:
            pf.holdings.extend(_parse_equity(rows))
        elif "fund" in title and "sip" not in title:
            pf.holdings.extend(_parse_funds(rows))

    # 2) content-driven fallback for free-form sheets ("Sheet1", side-by-side
    #    tables, header not on row 1, etc.)
    if not pf.holdings and not pf.sips:
        for _title, rows in sheets:
            for kind, hdr, data in _scan_generic(rows):
                mini = [hdr] + data
                if kind == "equity":
                    pf.holdings.extend(_parse_equity(mini))
                else:
                    htext = " ".join(str(h or "").lower() for h in hdr)
                    if "unit" in htext or "nav" in htext:
                        pf.holdings.extend(_parse_funds(mini))
                    else:
                        pf.sips.extend(_parse_sips(mini))
    return pf


_COL_KW = ("stock", "scrip", "share", "holding", "fund", "scheme", "qty",
           "quantity", "price", "cost", "invested", "unit", "nav", "amount",
           "value", "category", "nature", "sip", "folio", "risk", "sector",
           "ltp", "name", "isin")


def _is_hdr_cell(c) -> bool:
    if c is None:
        return False
    s = str(c).strip().lower()
    return bool(s) and any(k in s for k in _COL_KW)


def _scan_generic(rows: list[list]) -> list[tuple[str, list, list]]:
    """Detect table(s) in a free-form sheet without relying on the sheet name.

    Picks the row with the most header-like cells as the header, splits it into
    side-by-side column blocks (separated by empty columns), and classifies each
    block as 'equity' or 'fund'. Returns (kind, header_cells, data_rows).
    """
    best_i, best = None, 1
    for i, row in enumerate(rows):
        score = sum(1 for c in row if _is_hdr_cell(c))
        if score > best:
            best, best_i = score, i
    if best_i is None:
        return []

    header = rows[best_i]
    n = len(header)
    blocks: list[tuple[int, int]] = []
    j = 0
    while j < n:
        if header[j] is not None and str(header[j]).strip():
            k = j
            while k < n and header[k] is not None and str(header[k]).strip():
                k += 1
            blocks.append((j, k))
            j = k
        else:
            j += 1

    data_rows = rows[best_i + 1:]
    out: list[tuple[str, list, list]] = []
    for s, e in blocks:
        hdr = [header[c] for c in range(s, e)]
        htext = " ".join(str(h or "").lower() for h in hdr)
        if any(k in htext for k in ("stock", "scrip", "share")) and "fund" not in htext:
            kind = "equity"
        elif "fund" in htext or "scheme" in htext:
            kind = "fund"
        else:
            continue
        data = [[r[c] if c < len(r) else None for c in range(s, e)] for r in data_rows]
        out.append((kind, hdr, data))
    return out


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
    i_month = _find(hdr, "monthly", "sip amount", "sip (")
    i_verdict = _find(hdr, "verdict", "action")
    i_notes = _find(hdr, "note")
    out: list[Holding] = []
    for cells in rows[1:]:
        if _is_total_row(cells) or i_fund is None or not cells[i_fund]:
            continue
        monthly = _num(cells[i_month]) if i_month is not None else None
        if monthly is None:                      # e.g. amount embedded in "SIP of 10000 p.m."
            monthly = _sip_amount(cells)
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
    i_name = _find(hdr, "stock", "scrip", "share", "name")
    i_sec = _find(hdr, "sector", "theme", "industry")
    # average/buy cost first so "Cost Price" maps to cost, not current price
    i_avg = _find(hdr, "avg cost", "avg price", "avg buy", "buy price",
                  "cost price", "cost/unit", "cost")
    i_price = _find(hdr, "ltp", "cmp", "market price", "current price",
                    "last price", "mkt price", "price")
    if i_price is not None and i_price == i_avg:   # a lone "Cost Price" column
        i_price = None
    i_inv = _find(hdr, "invested", "value", "amount", "corpus")
    i_qty = _find(hdr, "qty", "quantity", "shares", "units")
    i_bdate = _find(hdr, "buy date", "purchase date", "acquired", "date")
    i_risk = _find(hdr, "risk")
    i_notes = _find(hdr, "note")
    out: list[Holding] = []
    for cells in rows[1:]:
        if _is_total_row(cells) or i_name is None or not cells[i_name]:
            continue
        qty = _num(cells[i_qty]) if i_qty is not None else None
        avg = _num(cells[i_avg]) if i_avg is not None else None
        inv = _num(cells[i_inv]) if i_inv is not None else None
        # quantity x average cost is the reliable cost basis (in rupees); fall
        # back to an explicit Invested column only when qty/avg aren't both given
        if qty and avg:
            invested = qty * avg
        else:
            invested = inv or 0.0
        out.append(Holding(
            name=str(cells[i_name]).strip(),
            asset_type=AssetType.EQUITY,
            invested=invested,
            sector=str(cells[i_sec]).strip() if i_sec is not None and cells[i_sec] else "Uncategorised",
            price=_num(cells[i_price]) if i_price is not None else None,
            quantity=qty,
            avg_cost=avg,
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
