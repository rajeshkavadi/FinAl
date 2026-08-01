"""Broker + CAS auto-sync: pull real holdings so nothing is hand-entered.

Two sources feed the same ``Portfolio``:

  * Broker (direct equity, with quantity + average cost + live price)
      - ``ZerodhaKiteProvider``   — Kite Connect /portfolio/holdings
      - ``BrokerCSVProvider``     — any broker's holdings CSV export
  * CAS (mutual funds, with units + NAV + value)
      - ``load_cas``              — CAMS/KFintech Consolidated Account Statement
                                    (PDF via a guarded extractor, or text/csv)

The *parsers* (JSON/CSV/text -> Holding) are pure and unit-tested offline. The
network call (Kite) and PDF extraction are isolated and best-effort, so this
runs live on your machine and is still testable here.

Nothing is auto-traded. This only *reads* holdings.
"""
from __future__ import annotations

import csv
import json
import re
import urllib.request
from pathlib import Path
from typing import Optional

from .models import AssetType, Holding, Portfolio

_UA = {"User-Agent": "portfolio-analyzer"}


def _num(v) -> Optional[float]:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(",", "").replace("₹", "").replace("INR", "").strip()
    try:
        return float(s)
    except ValueError:
        return None


# ==========================================================================
# Broker — Zerodha Kite Connect
# ==========================================================================
class ZerodhaKiteProvider:
    """Fetch equity holdings from Kite Connect.

    Auth uses the documented header ``Authorization: token <api_key>:<access_token>``.
    The access token is obtained via Kite's daily login flow (not handled here —
    pass a valid token). Endpoint: GET https://api.kite.trade/portfolio/holdings.
    """

    URL = "https://api.kite.trade/portfolio/holdings"

    def __init__(self, api_key: str, access_token: str, timeout: int = 20):
        self.api_key, self.access_token, self.timeout = api_key, access_token, timeout

    def _get(self) -> Optional[dict]:
        req = urllib.request.Request(self.URL, headers={
            **_UA,
            "X-Kite-Version": "3",
            "Authorization": f"token {self.api_key}:{self.access_token}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return json.loads(r.read())
        except Exception:
            return None

    def holdings(self) -> list[Holding]:
        data = self._get()
        return parse_kite_holdings(data) if data else []


def parse_kite_holdings(payload: dict) -> list[Holding]:
    """Map a Kite /portfolio/holdings response to Holdings (pure)."""
    rows = payload.get("data", payload) if isinstance(payload, dict) else payload
    out: list[Holding] = []
    for h in rows or []:
        qty = _num(h.get("quantity")) or 0.0
        # include long-term/collateral quantities if present
        qty += _num(h.get("t1_quantity")) or 0.0
        avg = _num(h.get("average_price"))
        last = _num(h.get("last_price"))
        if qty <= 0:
            continue
        out.append(Holding(
            name=h.get("tradingsymbol") or h.get("symbol") or "?",
            asset_type=AssetType.EQUITY,
            invested=(qty * avg) if avg else 0.0,
            price=last,
            quantity=qty,
            avg_cost=avg,
            symbol=h.get("tradingsymbol"),
            isin=h.get("isin"),
        ))
    return out


# ==========================================================================
# Broker — generic CSV export
# ==========================================================================
def _find(hdr: list[str], *needles: str) -> Optional[int]:
    low = [(h or "").strip().lower() for h in hdr]
    for n in needles:
        for i, h in enumerate(low):
            if n in h:
                return i
    return None


class BrokerCSVProvider:
    """Parse a broker's holdings CSV export (Zerodha console, Groww, etc.)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def holdings(self) -> list[Holding]:
        with open(self.path, newline="", encoding="utf-8-sig") as fh:
            rows = [r for r in csv.reader(fh) if any(c.strip() for c in r)]
        if not rows:
            return []
        hdr = rows[0]
        i_sym = _find(hdr, "symbol", "tradingsymbol", "instrument", "stock", "name")
        i_qty = _find(hdr, "quantity", "qty", "shares", "units")
        i_avg = _find(hdr, "average", "avg", "buy price", "cost")
        i_last = _find(hdr, "last", "ltp", "close", "market price", "current")
        i_isin = _find(hdr, "isin")
        out: list[Holding] = []
        for r in rows[1:]:
            if i_sym is None or i_sym >= len(r) or not r[i_sym].strip():
                continue
            qty = _num(r[i_qty]) if i_qty is not None and i_qty < len(r) else None
            avg = _num(r[i_avg]) if i_avg is not None and i_avg < len(r) else None
            last = _num(r[i_last]) if i_last is not None and i_last < len(r) else None
            if not qty or qty <= 0:
                continue
            out.append(Holding(
                name=r[i_sym].strip(),
                asset_type=AssetType.EQUITY,
                invested=(qty * avg) if avg else 0.0,
                price=last, quantity=qty, avg_cost=avg,
                symbol=r[i_sym].strip(),
                isin=r[i_isin].strip() if i_isin is not None and i_isin < len(r) else None,
            ))
        return out


# ==========================================================================
# CAS — Consolidated Account Statement (mutual funds)
# ==========================================================================
_CLOSING = re.compile(r"closing\s+unit\s+balance[:\s]+([\d,]+\.?\d*)", re.I)
_NAV = re.compile(r"nav\s+on[^:]*:\s*(?:inr|rs\.?|₹)?\s*([\d,]+\.?\d*)", re.I)
_MKT = re.compile(r"market\s+value[^:]*:\s*(?:inr|rs\.?|₹)?\s*([\d,]+\.?\d*)", re.I)
_COST = re.compile(r"(?:total\s+)?cost\s+value[^:]*:\s*(?:inr|rs\.?|₹)?\s*([\d,]+\.?\d*)", re.I)
_ISIN = re.compile(r"\b(INF[A-Z0-9]{9})\b")
_SCHEME_HINT = re.compile(r"(fund|scheme|plan|growth|dividend|idcw|equity|flexi|"
                          r"large|mid|small|cap|debt|liquid|hybrid|index)", re.I)


def parse_cas_text(text: str) -> list[Holding]:
    """Parse CAMS/KFintech detailed CAS text into MF Holdings.

    Heuristic: track the most recent scheme-name line, then when a
    'Closing Unit Balance' block is seen, emit a holding using the nearby
    NAV / Market Value / Cost Value. Tuned for the detailed CAS layout; verify
    units/values against your statement.
    """
    lines = [ln.strip() for ln in text.splitlines()]
    out: list[Holding] = []
    scheme = ""
    isin = None
    for i, ln in enumerate(lines):
        if not ln:
            continue
        m_isin = _ISIN.search(ln)
        if m_isin:
            isin = m_isin.group(1)
        # a plausible scheme name: has fund-ish words, not a data line
        if _SCHEME_HINT.search(ln) and not _CLOSING.search(ln) and len(ln) > 8 \
                and not ln.lower().startswith(("nav", "market", "cost", "date")):
            # keep the longest recent candidate as the scheme name
            scheme = re.sub(r"\s{2,}", " ", ln).strip()
        if _CLOSING.search(ln):
            window = " ".join(lines[i:i + 4])   # NAV/value often follow
            units = _num(_CLOSING.search(ln).group(1))
            nav = _num(_NAV.search(window).group(1)) if _NAV.search(window) else None
            mkt = _num(_MKT.search(window).group(1)) if _MKT.search(window) else None
            cost = _num(_COST.search(window).group(1)) if _COST.search(window) else None
            if units and units > 0:
                invested = cost if cost else (mkt or 0.0)
                out.append(Holding(
                    name=scheme or "Unknown scheme",
                    asset_type=AssetType.MUTUAL_FUND,
                    invested=invested or 0.0,
                    category="", sector="Mutual Fund",
                    quantity=units, price=nav, isin=isin))
                isin = None
    return out


def read_cas_pdf(path: str | Path, password: str | None = None) -> str:
    """Extract text from a CAS PDF. Requires pdfplumber or pypdf (guarded)."""
    p = Path(path)
    try:
        import pdfplumber
        with pdfplumber.open(p, password=password or "") as pdf:
            return "\n".join((pg.extract_text() or "") for pg in pdf.pages)
    except ImportError:
        pass
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(p))
        if reader.is_encrypted:
            reader.decrypt(password or "")
        return "\n".join((pg.extract_text() or "") for pg in reader.pages)
    except ImportError as e:
        raise RuntimeError(
            "CAS PDF parsing needs 'pdfplumber' or 'pypdf' "
            "(pip install pdfplumber). Or export the CAS to text/csv.") from e


def load_cas(path: str | Path, password: str | None = None) -> list[Holding]:
    p = Path(path)
    if p.suffix.lower() == ".pdf":
        return parse_cas_text(read_cas_pdf(p, password))
    return parse_cas_text(p.read_text(encoding="utf-8", errors="ignore"))


# ==========================================================================
# Orchestration
# ==========================================================================
def build_portfolio(*, broker=None, cas_path=None, cas_password=None) -> Portfolio:
    """Assemble a Portfolio from a broker provider and/or a CAS file."""
    pf = Portfolio(meta={"source": "auto-sync"})
    if broker is not None:
        pf.holdings.extend(broker.holdings())
    if cas_path:
        pf.holdings.extend(load_cas(cas_path, cas_password))
    return pf


def to_csv(pf: Portfolio, equity_path: str | Path, funds_path: str | Path) -> None:
    """Write normalized equity + funds CSVs that run.py / monitor.py can load."""
    eq = pf.equities()
    if eq:
        with open(equity_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Stock", "Sector/Theme", "Quantity", "Price", "Invested",
                        "Avg Cost", "Risk Flag", "Notes"])
            for h in eq:
                w.writerow([h.name, h.sector, h.quantity or "", h.price or "",
                            round(h.invested, 2), h.avg_cost or "", h.risk_flag or "", ""])
    funds = pf.funds()
    if funds:
        with open(funds_path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["Fund", "Category", "Units", "NAV", "Invested", "ISIN"])
            for h in funds:
                w.writerow([h.name, h.category, h.quantity or "", h.price or "",
                            round(h.invested, 2), h.isin or ""])
