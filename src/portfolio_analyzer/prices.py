"""Live price / NAV providers.

These are best-effort and optional. On a normal internet connection they enrich
holdings with live equity prices (Yahoo Finance) and MF NAVs (AMFI). When the
network is unavailable or a symbol is unknown, the holding keeps its imported
price and the analyzer silently falls back to cost basis. No provider is
required for the analyzer to run.

AMFI (https://www.amfiindia.com/spages/NAVAll.txt) publishes the end-of-day NAV
for every Indian mutual-fund scheme, free and without an API key. Matching a
user's informally-typed fund name ("UTI Flexi Cap Fund Direct Growth") to
AMFI's canonical name ("UTI Flexi Cap Fund - Direct Plan - Growth") is the only
hard part, handled by a token-overlap matcher that respects the Direct/Regular
and Growth/IDCW distinctions so we never mark a position with the wrong plan.
"""
from __future__ import annotations

import json
import re
import urllib.request
from typing import Optional

from .models import AssetType, Portfolio

_UA = {"User-Agent": "Mozilla/5.0 (portfolio-analyzer)"}

# words that carry no discriminating power in a scheme name
_STOP = {"fund", "plan", "option", "scheme", "the", "of", "and", "growth",
         "idcw", "dividend", "payout", "reinvestment", "reinvest", "direct",
         "regular", "mutual"}
_IDCW = {"idcw", "dividend", "payout", "reinvestment", "reinvest"}


def _get(url: str, timeout: int = 15) -> Optional[bytes]:
    try:
        req = urllib.request.Request(url, headers=_UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return None


def _tokens(name: str) -> set[str]:
    """Significant (non-stopword, non-flag) tokens of a scheme name."""
    words = re.sub(r"[^a-z0-9]+", " ", name.lower()).split()
    return {w for w in words if w not in _STOP and len(w) > 1}


def _plan(name: str) -> Optional[str]:
    low = name.lower()
    if "direct" in low:
        return "direct"
    if "regular" in low:
        return "regular"
    return None


def _option(name: str) -> Optional[str]:
    low = re.sub(r"[^a-z0-9]+", " ", name.lower())
    if "growth" in low:
        return "growth"
    if any(w in low.split() for w in _IDCW):
        return "idcw"
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

    One HTTP call fetches the whole universe; we index it by ISIN and keep the
    parsed scheme catalogue (name -> nav) for fuzzy name matching.
    """

    URL = "https://www.amfiindia.com/spages/NAVAll.txt"

    def __init__(self) -> None:
        self._by_isin: dict[str, float] = {}
        # catalogue rows: (canonical_name, nav, token_set, plan, option)
        self._catalogue: list[tuple[str, float, set[str], Optional[str], Optional[str]]] = []
        self.as_of: Optional[str] = None
        self.loaded = False

    # -- ingestion ------------------------------------------------------
    def _ingest(self, text: str) -> None:
        for line in text.splitlines():
            parts = line.split(";")
            if len(parts) < 6:
                continue
            _code, isin1, isin2, name, nav, date = parts[:6]
            try:
                nav_f = float(nav)
            except ValueError:
                continue
            name = name.strip()
            for isin in (isin1, isin2):
                if isin.strip():
                    self._by_isin[isin.strip()] = nav_f
            self._catalogue.append(
                (name, nav_f, _tokens(name), _plan(name), _option(name)))
            if date.strip():
                self.as_of = date.strip()
        self.loaded = bool(self._catalogue)

    def load(self) -> bool:
        raw = _get(self.URL, timeout=30)
        if not raw:
            return False
        self._ingest(raw.decode("utf-8", "ignore"))
        return self.loaded

    def load_text(self, text: str) -> bool:
        """Ingest an already-fetched feed (used by tests / offline caches)."""
        self._ingest(text)
        return self.loaded

    # -- lookup ---------------------------------------------------------
    def nav(self, *, isin: str | None = None, name: str | None = None) -> Optional[float]:
        if isin and isin.strip() in self._by_isin:
            return self._by_isin[isin.strip()]
        if not name:
            return None
        q_tokens = _tokens(name)
        if not q_tokens:
            return None
        q_plan, q_option = _plan(name), _option(name)

        best: Optional[tuple[float, float]] = None  # (score, nav)
        for cname, cnav, ctoks, cplan, copt in self._catalogue:
            # never cross the Direct/Regular or Growth/IDCW divide when both sides state it
            if q_plan and cplan and q_plan != cplan:
                continue
            if q_option and copt and q_option != copt:
                continue
            if not ctoks:
                continue
            shared = q_tokens & ctoks
            if not shared:
                continue
            # fraction of the query's tokens found, tie-broken by how tightly
            # the candidate matches (penalise extra tokens on the AMFI side)
            coverage = len(shared) / len(q_tokens)
            tightness = len(shared) / len(ctoks)
            score = coverage + 0.25 * tightness
            # both plan and option agreeing is a strong signal
            if q_plan and cplan == q_plan:
                score += 0.15
            if q_option and copt == q_option:
                score += 0.15
            if coverage >= 0.6 and (best is None or score > best[0]):
                best = (score, cnav)
        return best[1] if best else None


def enrich_live(pf: Portfolio, *, equities: bool = True, funds: bool = True,
                _nav_provider: "AMFINavProvider | None" = None) -> dict:
    """Attach live prices/NAVs in place. Returns a status report for the UI.

    ``_nav_provider`` lets callers (and tests) inject a pre-loaded provider
    instead of hitting the network.
    """
    status = {
        "equity_updated": 0, "equity_total": 0,
        "nav_updated": 0, "nav_total": 0,
        "as_of": None, "unmatched": [], "errors": [],
    }

    if equities:
        yp = YahooEquityProvider()
        for h in pf.holdings:
            if h.asset_type == AssetType.EQUITY and h.symbol:
                status["equity_total"] += 1
                p = yp.price(h.symbol)
                if p is not None:
                    h.price = p
                    status["equity_updated"] += 1

    if funds:
        mf = [h for h in pf.holdings if h.asset_type == AssetType.MUTUAL_FUND]
        status["nav_total"] = len(mf)
        if mf:
            ap = _nav_provider or AMFINavProvider()
            ok = ap.loaded or ap.load()
            if ok:
                status["as_of"] = ap.as_of
                for h in mf:
                    nav = ap.nav(isin=h.isin, name=h.name)
                    if nav is not None:
                        h.price = nav
                        status["nav_updated"] += 1
                    else:
                        status["unmatched"].append(h.name)
            else:
                status["errors"].append("AMFI NAV feed unreachable")
    return status
