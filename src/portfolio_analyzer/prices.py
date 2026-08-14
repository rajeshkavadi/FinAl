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
    """Live-ish price for an Indian stock via Yahoo Finance's public API.

    Two steps, because a portfolio tracker usually holds a *company name*
    ("Arman Financial Services"), not a Yahoo ticker ("ARMANFIN.NS"):

      resolve_symbol(name)  name  -> best NSE/BSE ticker  (Yahoo search)
      price(ticker)         ticker -> latest regular-market price

    Both are best-effort and short-timeout so a blocked/slow network can't hang
    the dashboard. Results are cached for the process lifetime.
    """

    CHART = ("https://query1.finance.yahoo.com/v8/finance/chart/"
             "{tk}?interval=1d&range=1d")
    SEARCH = ("https://query1.finance.yahoo.com/v1/finance/search"
              "?q={q}&quotesCount=8&newsCount=0")

    def __init__(self, timeout: int = 6) -> None:
        self.timeout = timeout
        self._sym_cache: dict[str, Optional[str]] = {}

    # -- name -> ticker -------------------------------------------------
    def resolve_symbol(self, name: str) -> Optional[str]:
        key = name.strip().lower()
        if key in self._sym_cache:
            return self._sym_cache[key]
        result = self._resolve_uncached(name)
        self._sym_cache[key] = result
        return result

    def _resolve_uncached(self, name: str) -> Optional[str]:
        import urllib.parse
        raw = _get(self.SEARCH.format(q=urllib.parse.quote(name)), self.timeout)
        if not raw:
            return None
        return self.parse_search(raw)

    @staticmethod
    def parse_search(raw: bytes) -> Optional[str]:
        """Pick the best Indian-exchange ticker from a Yahoo search payload.

        Prefers NSE (.NS) over BSE (.BO); ignores non-equity / non-India hits.
        """
        try:
            quotes = json.loads(raw).get("quotes", [])
        except Exception:
            return None
        nse = bo = None
        for q in quotes:
            sym = q.get("symbol", "")
            if q.get("quoteType") not in (None, "EQUITY"):
                continue
            if sym.endswith(".NS") and nse is None:
                nse = sym
            elif sym.endswith(".BO") and bo is None:
                bo = sym
        return nse or bo

    # -- ticker -> price ------------------------------------------------
    def price(self, ticker: str) -> Optional[float]:
        # bare symbol (from a user's Symbol column) -> assume NSE
        tk = ticker if ("." in ticker) else f"{ticker}.NS"
        raw = _get(self.CHART.format(tk=tk), self.timeout)
        if not raw:
            return None
        try:
            meta = json.loads(raw)["chart"]["result"][0]["meta"]
            p = meta.get("regularMarketPrice")
            return float(p) if p is not None else None
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
        # the dump is ~8 MB; give it room and one retry before giving up
        for timeout in (45, 45):
            raw = _get(self.URL, timeout=timeout)
            if raw:
                self._ingest(raw.decode("utf-8", "ignore"))
                if self.loaded:
                    return True
        return False

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


class MFApiProvider:
    """Per-scheme NAV from the free mfapi.in mirror (small JSON responses).

    Used as a fallback when the 8 MB AMFI dump can't be reached — a home/office
    firewall sometimes blocks amfiindia.com but not api.mfapi.in. Two small
    calls per fund (search + latest), so it's only worth it for a handful.
    """

    SEARCH = "https://api.mfapi.in/mf/search?q={q}"
    LATEST = "https://api.mfapi.in/mf/{code}/latest"

    def __init__(self, timeout: int = 8) -> None:
        self.timeout = timeout
        self.as_of: Optional[str] = None

    def nav(self, *, isin: str | None = None, name: str | None = None) -> Optional[float]:
        if not name:
            return None
        import urllib.parse
        raw = _get(self.SEARCH.format(q=urllib.parse.quote(name)), self.timeout)
        if not raw:
            return None
        code = self._best_code(raw, name)
        if code is None:
            return None
        latest = _get(self.LATEST.format(code=code), self.timeout)
        if not latest:
            return None
        return self._parse_latest(latest)

    @staticmethod
    def _best_code(raw: bytes, query: str) -> Optional[int]:
        try:
            rows = json.loads(raw)
        except Exception:
            return None
        q_tokens, q_plan, q_option = _tokens(query), _plan(query), _option(query)
        best = None
        for row in rows:
            nm = row.get("schemeName", "")
            if q_plan and _plan(nm) and _plan(nm) != q_plan:
                continue
            if q_option and _option(nm) and _option(nm) != q_option:
                continue
            shared = q_tokens & _tokens(nm)
            if not shared:
                continue
            score = len(shared) / max(len(q_tokens), 1)
            if score >= 0.6 and (best is None or score > best[0]):
                best = (score, row.get("schemeCode"))
        return best[1] if best else None

    def _parse_latest(self, raw: bytes) -> Optional[float]:
        try:
            data = json.loads(raw)
            row = data["data"][0]
            self.as_of = row.get("date") or self.as_of
            return float(row["nav"])
        except Exception:
            return None


def enrich_live(pf: Portfolio, *, equities: bool = True, funds: bool = True,
                _nav_provider: "AMFINavProvider | None" = None,
                _equity_provider: "YahooEquityProvider | None" = None,
                _mfapi_provider: "MFApiProvider | None" = None) -> dict:
    """Attach live prices/NAVs in place. Returns a status report for the UI.

    ``_nav_provider`` / ``_equity_provider`` / ``_mfapi_provider`` let callers
    (and tests) inject pre-loaded providers instead of hitting the network.
    """
    status = {
        "equity_updated": 0, "equity_total": 0,
        "nav_updated": 0, "nav_total": 0,
        "as_of": None, "unmatched": [], "errors": [],
    }

    status["equity_no_symbol"] = []
    status["equity_no_quote"] = []
    if equities:
        eq = [h for h in pf.holdings if h.asset_type == AssetType.EQUITY]
        status["equity_total"] = len(eq)
        if eq:
            yp = _equity_provider or YahooEquityProvider()

            def _one(h) -> str:
                # a Symbol/Ticker column wins; otherwise resolve the name
                tk = h.symbol or yp.resolve_symbol(h.name)
                if not tk:
                    return "no_symbol"      # can't even identify the stock
                p = yp.price(tk)
                if p is None:
                    return "no_quote"       # known ticker, but no live data (SME/unlisted on source)
                h.price = p
                h.symbol = tk               # remember what we resolved to
                return "ok"

            # fetch in parallel with a bounded pool so N stocks take ~one
            # round-trip, not N, and a slow name never stalls the page
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(eq))) as ex:
                results = list(ex.map(_one, eq))
            status["equity_updated"] = sum(1 for r in results if r == "ok")
            status["equity_no_symbol"] = [h.name for h, r in zip(eq, results) if r == "no_symbol"]
            status["equity_no_quote"] = [h.name for h, r in zip(eq, results) if r == "no_quote"]
            # kept for back-compat: any stock without a live price
            status["equity_unmatched"] = status["equity_no_symbol"] + status["equity_no_quote"]

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
                # AMFI dump unreachable — fall back to the per-scheme mirror
                status["errors"].append("AMFI dump unreachable; used mfapi.in fallback")
                mp = _mfapi_provider or MFApiProvider()

                def _one_nav(h):
                    return h, mp.nav(isin=h.isin, name=h.name)

                from concurrent.futures import ThreadPoolExecutor
                with ThreadPoolExecutor(max_workers=min(8, len(mf))) as ex:
                    for h, nav in ex.map(_one_nav, mf):
                        if nav is not None:
                            h.price = nav
                            status["nav_updated"] += 1
                        else:
                            status["unmatched"].append(h.name)
                status["as_of"] = mp.as_of
                if status["nav_updated"] == 0:
                    status["errors"][-1] = "MF NAV feeds unreachable (AMFI + mfapi.in)"
    return status
