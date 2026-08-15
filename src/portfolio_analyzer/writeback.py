"""Write live prices back into the user's own workbook.

After enrichment we know each holding's live price. This re-opens the exact
xlsx the user uploaded and fills the live figure into their price column (e.g.
the 'Current price' column of the Stocks sheet) plus a 'Current value' column
when present, then returns the updated workbook as bytes — so the user gets
their sheet back with prices filled in, rather than typing them by hand.
"""
from __future__ import annotations

import io
from pathlib import Path

from .loader import _find
from .models import Portfolio

# columns we treat as the live-updatable price (NOT the average/cost column)
_PRICE_NEEDLES = ("ltp", "cmp", "market price", "current price", "last price",
                  "mkt price", "nav", "price")
_AVG_NEEDLES = ("avg cost", "avg price", "avg buy", "buy price", "cost price",
                "cost/unit", "cost")
_VALUE_NEEDLES = ("current value", "cur value", "market value", "current val")


def fill_live_prices(src_path: str | Path, pf: Portfolio) -> bytes | None:
    """Return the workbook with live prices/values filled in, or None if nothing
    could be updated (no matching rows / no price column)."""
    import openpyxl

    by_name = {h.name.strip().lower(): h for h in pf.holdings if h.price is not None}
    if not by_name:
        return None
    wb = openpyxl.load_workbook(src_path)
    updated = 0

    for ws in wb.worksheets:
        rows = list(ws.iter_rows())
        if not rows:
            continue
        # locate the header row (first row that has a name-like column)
        hdr_idx = None
        for i, row in enumerate(rows[:6]):
            vals = [(str(c.value) if c.value is not None else "") for c in row]
            if _find(vals, "stock", "scrip", "share", "fund", "scheme", "name") is not None:
                hdr_idx, hdr_vals = i, vals
                break
        if hdr_idx is None:
            continue

        i_name = _find(hdr_vals, "stock", "scrip", "share", "fund", "scheme", "name")
        i_avg = _find(hdr_vals, *_AVG_NEEDLES)
        i_price = _find(hdr_vals, *_PRICE_NEEDLES)
        if i_price is not None and i_price == i_avg:   # a lone cost column, not a live price
            i_price = None
        i_val = _find(hdr_vals, *_VALUE_NEEDLES)
        if i_price is None and i_val is None:
            continue

        for row in rows[hdr_idx + 1:]:
            if i_name >= len(row) or row[i_name].value is None:
                continue
            h = by_name.get(str(row[i_name].value).strip().lower())
            if h is None:
                continue
            if i_price is not None and i_price < len(row):
                row[i_price].value = round(float(h.price), 4)
                updated += 1
            if i_val is not None and i_val < len(row) and h.valued_on_market:
                row[i_val].value = round(float(h.current_value), 2)

    if not updated:
        return None
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
