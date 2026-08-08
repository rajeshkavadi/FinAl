"""Capital-Gains statement importer (realized gains for a past year).

A CG report is *not* a holdings statement — it lists trades you already sold,
with buy/sell dates and the broker's computed gain, classified short/long term.
This module parses one into ``CGRecord`` rows, reconciles the per-term totals,
and (using the fiscal-year-aware tax engine) recomputes the tax the broker's
gains imply — a useful cross-check at filing time.

Tuned for the **SBICAP Securities** consolidated CG report layout (the columns
Scrip / ISIN / Qty / Sale date+value / Purchase date+value / Capital gain, under
"Short Term Gain…" / "Long Term Gain…" section headers). Robust to the exact
number of per-share price columns. Other brokers' layouts can be added behind
the same ``parse_cg_text`` interface.

Parsers are pure (text → records) and unit-tested; PDF extraction is isolated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Optional

_ISIN = re.compile(r"IN[A-Z0-9]{10}")           # search anywhere in a line
_AMOUNT = re.compile(r"^-?\d[\d,]*\.\d{2}$")
_DATE = re.compile(r"^(\d{1,2})[-\s]?([A-Za-z]{3})[-\s]?(\d{2,4})$")
_QTY = re.compile(r"^\d[\d,]*(\.\d+)?$")

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# section header -> canonical term
_TERM_HEADERS = [
    ("long term", "LTCG"),
    ("short term", "STCG"),
    ("speculative", "SPECULATIVE"),
]


def _num(s: str) -> Optional[float]:
    try:
        return float(str(s).replace(",", ""))
    except ValueError:
        return None


def _parse_date(tok: str) -> Optional[date]:
    m = _DATE.match(tok.strip())
    if not m:
        return None
    d, mon, y = m.groups()
    mon_i = _MONTHS.get(mon.lower())
    if not mon_i:
        return None
    yr = int(y)
    if yr < 100:
        yr += 2000
    try:
        return date(yr, mon_i, int(d))
    except ValueError:
        return None


@dataclass
class CGRecord:
    scrip: str
    isin: str
    term: str                 # STCG | LTCG | SPECULATIVE
    quantity: Optional[float]
    buy_date: Optional[date]
    sell_date: Optional[date]
    buy_value: Optional[float]
    sell_value: Optional[float]
    gain: Optional[float]


@dataclass
class CGResult:
    records: list[CGRecord] = field(default_factory=list)

    def by_term(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for r in self.records:
            out[r.term] = out.get(r.term, 0.0) + (r.gain or 0.0)
        return out

    @property
    def total_gain(self) -> float:
        return sum((r.gain or 0.0) for r in self.records)


def _term_of(line: str) -> Optional[str]:
    low = line.lower()
    for needle, term in _TERM_HEADERS:
        if needle in low:
            return term
    return None


def _is_name(line: str) -> bool:
    """A line that looks like (part of) a scrip name, not data/headers."""
    low = line.lower()
    if _ISIN.search(line) or _term_of(line) or low.startswith(("total", "page", "scrip")):
        return False
    return bool(re.search(r"[A-Za-z]", line)) and not _AMOUNT.match(line)


def parse_cg_text(text: str) -> CGResult:
    """Parse CG-report text into realized-gain records.

    Handles the record laid out on one line starting at (or containing) the
    ISIN — ``ISIN qty sell-date sell-value buy-date …prices… buy-value gain`` —
    with the scrip name on neighbouring lines, and also the multi-line variant
    where each field is on its own line. A record is only accepted when it has
    **two dates** (buy + sell), which cleanly excludes dividend/interest rows.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    res = CGResult()
    term = "STCG"
    for idx, line in enumerate(lines):
        th = _term_of(line)
        if th and "gain" in line.lower():        # section header
            term = th
            continue
        m = _ISIN.search(line)
        if not m:
            continue
        isin = m.group()

        # value tokens: the rest of this line, plus following lines if this line
        # only carried the ISIN (the multi-line layout)
        tokens = line[m.end():].split()
        if sum(1 for t in tokens if _parse_date(t)) < 2:
            j = idx + 1
            while j < len(lines) and len(tokens) < 12:
                nxt = lines[j]
                if _ISIN.search(nxt) or (_term_of(nxt) and "gain" in nxt.lower()) \
                        or nxt.lower().startswith(("total", "grand total")):
                    break
                tokens += nxt.split()
                j += 1

        dates = [d for d in (_parse_date(t) for t in tokens) if d]
        if len(dates) < 2:                        # not a CG row (e.g. dividend)
            continue
        amounts = [_num(t) for t in tokens if _AMOUNT.match(t)]
        if len(amounts) < 2:
            continue
        qty = next((_num(t) for t in tokens if _QTY.match(t) and not _AMOUNT.match(t)), None)

        scrip = line[:m.start()].strip()
        if not scrip:                             # ISIN at line start → name is on neighbours
            parts = []
            if idx and _is_name(lines[idx - 1]):
                parts.append(lines[idx - 1])
            if idx + 1 < len(lines) and _is_name(lines[idx + 1]):
                parts.append(lines[idx + 1])
            scrip = " ".join(parts) or isin

        res.records.append(CGRecord(
            scrip=scrip, isin=isin, term=term, quantity=qty,
            sell_date=dates[0], buy_date=dates[1],
            sell_value=amounts[0], buy_value=amounts[-2], gain=amounts[-1]))
    return res


# --------------------------------------------------------------------------
def pdf_text(path: str | Path, password: str | None = None) -> str:
    """Extract text from a CG PDF (guarded — needs pdfplumber or pypdf)."""
    p = Path(path)
    try:
        import pdfplumber
        with pdfplumber.open(p, password=password or "") as pdf:
            return "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
        r = PdfReader(str(p))
        if r.is_encrypted:
            r.decrypt(password or "")
        return "\n".join((pg.extract_text() or "") for pg in r.pages)
    except ImportError as e:
        raise RuntimeError("CG PDF parsing needs 'pdfplumber' or 'pypdf' "
                           "(pip install pypdf). Or export the report to text.") from e


def load_cg(path: str | Path, password: str | None = None) -> CGResult:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return parse_cg_text(pdf_text(p, password))
    return parse_cg_text(p.read_text(encoding="utf-8", errors="ignore"))


# --------------------------------------------------------------------------
def recompute_tax(result: CGResult, slab_rate: Optional[float] = None):
    """Recompute the tax implied by the realized gains, per each trade's own
    fiscal-year regime (rates follow the sell date). Returns a dict summary.
    """
    from .tax import AssetClass, TaxConfig, estimate_switch_tax

    stcg_tax = ltcg_tax = 0.0
    per_year: dict = {}
    # group records by the regime date so each batch gets its year's rates + exemption
    for r in result.records:
        if r.gain is None:
            continue
        asof = r.sell_date or date.today()
        cfg = TaxConfig.for_date(asof, slab_rate=slab_rate)
        # reuse the term the broker assigned (authoritative for the period)
        key = "pre-2024-07-23" if cfg.equity_ltcg_rate == 0.10 else "post-2024-07-23"
        bucket = per_year.setdefault(key, {"cfg": cfg, "stcg": 0.0, "ltcg": 0.0})
        if r.term == "STCG":
            bucket["stcg"] += r.gain
        elif r.term == "LTCG":
            bucket["ltcg"] += r.gain

    total_tax = 0.0
    details = []
    for key, b in per_year.items():
        cfg = b["cfg"]
        cess = 1.0 + cfg.cess
        st = max(0.0, b["stcg"]) * cfg.equity_stcg_rate * cess
        lt_taxable = max(0.0, b["ltcg"] - cfg.ltcg_exemption)
        lt = lt_taxable * cfg.equity_ltcg_rate * cess
        stcg_tax += st
        ltcg_tax += lt
        total_tax += st + lt
        details.append({
            "regime": key, "stcg_gain": b["stcg"], "ltcg_gain": b["ltcg"],
            "stcg_rate": cfg.equity_stcg_rate, "ltcg_rate": cfg.equity_ltcg_rate,
            "ltcg_exemption": cfg.ltcg_exemption,
            "stcg_tax": st, "ltcg_tax": lt,
        })
    return {"stcg_tax": stcg_tax, "ltcg_tax": ltcg_tax,
            "total_tax": total_tax, "by_regime": details}
