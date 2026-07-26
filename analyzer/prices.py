"""Live price / NAV providers.

These are best-effort and optional. On a normal internet connection they enrich
holdings with live equity prices (Yahoo Finance) and MF NAVs (AMFI). When the
network is unavailable or a symbol is unknown, the holding keeps its imported
price and the analyzer silently falls back to cost basis. No provider is
required for the analyzer to run.
"""
from __future__ import annotations

import json
import urllib.request
from typing import Optional

from .models import AssetType, Portfolio

_UA = {"User-Agent": "Mozilla/5.0 (portfolio-analyzer)"}


def _get(url: str, timeout: int = 15) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


# --------------------------------------------------------------------------
class YahooEquityProvider:
    """Latest close for an NSE symbol via Yahoo Finance's public chart API."""

    URL = ("https://query1.finance.yahoo.com/v8/finance/chart/"
           "{sym}.NS?interval=1d&range=1d")

    def price(self, symbol: str) -> Optional[float]:
        raw = _get(self.URL.format(sym=symbol))
        if not raw:
            return None
        try:
            data = json.loads(raw)
            meta = data["chart"]["result"][0]["meta"]
            return float(meta.get("regularMarketPrice"))
        except Exception:
            return None


class AMFINavProvider:
    """Daily NAVs for every Indian MF scheme from AMFI's public dump.

    One HTTP call fetches the whole universe; we index it by ISIN and by a
    normalised scheme name for lookup.
    """

    URL = "https://www.amfiindia.com/spages/NAVAll.txt"

    def __init__(self) -> None:
        self._by_isin: dict[str, float] = {}
        self._by_name: dict[str, float] = {}
        self.loaded = False

    def load(self) -> bool:
        raw = _get(self.URL, timeout=30)
        if not raw:
            return False
        for line in raw.decode("utf-8", "ignore").splitlines():
            parts = line.split(";")
            if len(parts) < 6:
                continue
            _code, isin1, isin2, name, nav, _date = parts[:6]
            try:
                nav_f = float(nav)
            except ValueError:
                continue
            for isin in (isin1, isin2):
                if isin.strip():
                    self._by_isin[isin.strip()] = nav_f
            self._by_name[name.strip().lower()] = nav_f
        self.loaded = bool(self._by_name)
        return self.loaded

    def nav(self, *, isin: str | None = None, name: str | None = None) -> Optional[float]:
        if isin and isin in self._by_isin:
            return self._by_isin[isin]
        if name:
            key = name.strip().lower()
            if key in self._by_name:
                return self._by_name[key]
            # loose contains-match on the fund name
            for k, v in self._by_name.items():
                if key in k:
                    return v
        return None


def enrich_live(pf: Portfolio, *, equities: bool = True, funds: bool = True) -> dict:
    """Attach live prices/NAVs in place. Returns a small status report."""
    status = {"equity_updated": 0, "nav_updated": 0, "errors": []}

    if equities:
        yp = YahooEquityProvider()
        for h in pf.holdings:
            if h.asset_type == AssetType.EQUITY and h.symbol:
                p = yp.price(h.symbol)
                if p is not None:
                    h.price = p
                    status["equity_updated"] += 1

    if funds:
        ap = AMFINavProvider()
        if ap.load():
            for h in pf.holdings:
                if h.asset_type == AssetType.MUTUAL_FUND:
                    nav = ap.nav(isin=h.isin, name=h.name)
                    if nav is not None:
                        h.price = nav
                        status["nav_updated"] += 1
        else:
            status["errors"].append("AMFI NAV feed unreachable")
    return status
