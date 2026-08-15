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
    def _chart_price(self, tk: str) -> Optional[float]:
        raw = _get(self.CHART.format(tk=tk), self.timeout)
        if not raw:
            return None
        try:
            meta = json.loads(raw)["chart"]["result"][0]["meta"]
            p = meta.get("regularMarketPrice")
            return float(p) if p is not None else None
        except Exception:
            return None

    def price(self, ticker: str) -> Optional[float]:
        return self.price_source(ticker)[0]

    def price_source(self, ticker: str) -> tuple[Optional[float], Optional[str]]:
        """Return (price, source-label). Tries, in order: Yahoo NSE, Yahoo BSE,
        NSE quote API, Screener.in. The label names which one answered."""
        if "." in ticker:
            p = self._chart_price(ticker)
            src = "BSE" if ticker.upper().endswith(".BO") else "NSE"
            return (p, src if p is not None else None)
        base = ticker.strip().upper()
        for suffix, label in ((".NS", "NSE"), (".BO", "BSE")):
            p = self._chart_price(base + suffix)
            if p is not None:
                return p, label
        # last resorts for SME scrips Yahoo doesn't carry (best-effort, silent)
        p = NSEQuoteProvider(self.timeout).price(base)
        if p is not None:
            return p, "NSE API"
        p = ScreenerProvider(self.timeout).price(base)
        if p is not None:
            return p, "Screener"
        return None, None


class NSEQuoteProvider:
    """Best-effort live quote from NSE India's public quote API.

    NSE requires a browser-like session (cookies primed from the home page)
    before the JSON endpoint responds. It often works from a home connection
    and is a useful fallback for SME scrips Yahoo doesn't carry, but it can be
    rate-limited/blocked — so every failure is silent and non-fatal.
    """

    HOME = "https://www.nseindia.com/"
    QUOTE = "https://www.nseindia.com/api/quote-equity?symbol={sym}"

    def __init__(self, timeout: int = 6) -> None:
        self.timeout = timeout

    def price(self, symbol: str) -> Optional[float]:
        import http.cookiejar
        import urllib.request
        try:
            jar = http.cookiejar.CookieJar()
            opener = urllib.request.build_opener(
                urllib.request.HTTPCookieProcessor(jar))
            headers = [("User-Agent", _UA["User-Agent"]),
                       ("Accept", "*/*"),
                       ("Referer", self.HOME)]
            opener.addheaders = headers
            opener.open(self.HOME, timeout=self.timeout).read()  # prime cookies
            import urllib.parse
            url = self.QUOTE.format(sym=urllib.parse.quote(symbol))
            data = opener.open(url, timeout=self.timeout).read()
            info = json.loads(data).get("priceInfo", {})
            p = info.get("lastPrice")
            return float(p) if p is not None else None
        except Exception:
            return None


class ScreenerProvider:
    """Best-effort current price by scraping screener.in's company page.

    Screener is symbol-addressable (/company/FLYSBS/) and lists the current
    price in the page HTML, so it's a handy last resort for NSE-SME scrips that
    Yahoo/NSE don't return. HTML scraping is inherently fragile (layout can
    change) and subject to the site's terms, so it's the final fallback only and
    fails silently.
    """

    URL = "https://www.screener.in/company/{sym}/"
    # "Current Price" label followed (soon after) by <span class="number">441</span>
    _RE = re.compile(
        r"Current\s*Price.{0,200}?<span[^>]*class=\"number\"[^>]*>\s*"
        r"([\d,]+(?:\.\d+)?)", re.I | re.S)

    def __init__(self, timeout: int = 8) -> None:
        self.timeout = timeout

    def price(self, symbol: str) -> Optional[float]:
        raw = _get(self.URL.format(sym=symbol.strip().upper()), self.timeout)
        if not raw:
            return None
        return self.parse_price(raw.decode("utf-8", "ignore"))

    @classmethod
    def parse_price(cls, html_text: str) -> Optional[float]:
        m = cls._RE.search(html_text)
        if not m:
            return None
        try:
            return float(m.group(1).replace(",", ""))
        except ValueError:
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

    HISTORY = "https://api.mfapi.in/mf/{code}"

    def resolve_code(self, name: str) -> Optional[int]:
        import urllib.parse
        raw = _get(self.SEARCH.format(q=urllib.parse.quote(name)), self.timeout)
        if not raw:
            return None
        return self._best_code(raw, name)

    def nav(self, *, isin: str | None = None, name: str | None = None) -> Optional[float]:
        if not name:
            return None
        code = self.resolve_code(name)
        if code is None:
            return None
        latest = _get(self.LATEST.format(code=code), self.timeout)
        if not latest:
            return None
        return self._parse_latest(latest)

    def series(self, name: str) -> list[tuple[str, float]]:
        """Full NAV history as [(dd-mm-YYYY, nav), ...], oldest first. Empty on failure."""
        code = self.resolve_code(name)
        if code is None:
            return []
        raw = _get(self.HISTORY.format(code=code), max(self.timeout, 12))
        if not raw:
            return []
        return self._parse_series(raw)

    @staticmethod
    def _parse_series(raw: bytes) -> list[tuple[str, float]]:
        try:
            data = json.loads(raw).get("data", [])
        except Exception:
            return []
        out = []
        for row in data:
            try:
                out.append((row["date"], float(row["nav"])))
            except (KeyError, ValueError, TypeError):
                continue
        out.reverse()   # mfapi returns newest-first; we want oldest-first
        return out

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


def _parse_ddmmyyyy(s: str):
    import datetime as _dt
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return _dt.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def estimate_units_from_sip(series: list[tuple[str, float]], monthly: float,
                            start, today=None) -> Optional[float]:
    """Approximate accumulated units from a monthly SIP against NAV history.

    Simulates buying ``monthly`` rupees of units on/after the SIP date each
    month from ``start`` to today, at the first available NAV on/after that day.
    This is an ESTIMATE — it assumes an unbroken monthly SIP and ignores lump
    sums or stopped months — but it's far better than nothing when the user
    hasn't entered actual units. Returns None if it can't be computed.
    """
    import datetime as _dt
    if not series or not monthly or start is None:
        return None
    today = today or _dt.date.today()
    # index navs by date for on/after lookup
    dated = [(d, nav) for d, nav in ((_parse_ddmmyyyy(s), n) for s, n in series) if d]
    if not dated:
        return None
    dated.sort()

    def nav_on_or_after(day):
        for d, nav in dated:
            if d >= day:
                return nav
        return None

    units = 0.0
    y, m = start.year, start.month
    day = min(start.day, 28)
    bought = 0
    while _dt.date(y, m, day) <= today and bought < 600:  # cap iterations
        nav = nav_on_or_after(_dt.date(y, m, day))
        if nav:
            units += monthly / nav
        bought += 1
        m += 1
        if m > 12:
            m = 1
            y += 1
    return units if units > 0 else None


def sparkline_svg(series: list[tuple[str, float]], *, months: int = 6,
                  width: int = 120, height: int = 28) -> str:
    """Tiny inline SVG line of the last ``months`` of NAV. '' if too little data."""
    if not series or len(series) < 2:
        return ""
    tail = series[-(months * 22):]        # ~22 trading days/month
    vals = [n for _, n in tail]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    pts = []
    for i, v in enumerate(vals):
        x = i / (n - 1) * (width - 2) + 1
        yv = height - 2 - (v - lo) / rng * (height - 4)
        pts.append(f"{x:.1f},{yv:.1f}")
    up = vals[-1] >= vals[0]
    color = "#067647" if up else "#b42318"
    return (f"<svg width='{width}' height='{height}' viewBox='0 0 {width} {height}' "
            f"preserveAspectRatio='none' style='vertical-align:middle'>"
            f"<polyline fill='none' stroke='{color}' stroke-width='1.5' points='{' '.join(pts)}'/>"
            f"</svg>")


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
                if hasattr(yp, "price_source"):
                    p, src = yp.price_source(tk)
                else:                        # injected test doubles expose only price()
                    p, src = yp.price(tk), None
                if p is None:
                    return "no_quote"       # known ticker, but no live data (SME/unlisted on source)
                h.price = p
                h.symbol = tk               # remember what we resolved to
                h.price_source = src
                return "ok"

            # fetch in parallel with a bounded pool so N stocks take ~one
            # round-trip, not N, and a slow name never stalls the page
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(eq))) as ex:
                results = list(ex.map(_one, eq))
            status["equity_updated"] = sum(1 for r in results if r == "ok")
            status["equity_live"] = [h.name for h, r in zip(eq, results) if r == "ok"]
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

            # NAV history (mfapi.in) → 6-month sparkline + units-from-SIP estimate.
            # Best-effort and parallel; silently skipped when the mirror is blocked.
            status.setdefault("nav_spark", {})
            status.setdefault("units_estimated", [])
            hp = _mfapi_provider or MFApiProvider()

            def _hist(h):
                return h, hp.series(h.name)

            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(8, len(mf))) as ex:
                for h, series in ex.map(_hist, mf):
                    if not series:
                        continue
                    spark = sparkline_svg(series)
                    if spark:
                        status["nav_spark"][h.name] = spark
                    # estimate units when the user gave a monthly SIP + start date
                    # but no units, so the fund can still be valued
                    if h.quantity is None and h.monthly_sip and h.sip_start:
                        est = estimate_units_from_sip(series, h.monthly_sip, h.sip_start)
                        if est:
                            h.quantity = est
                            h.units_estimated = True
                            status["units_estimated"].append(h.name)
                        if h.price is None and series:
                            h.price = series[-1][1]
    return status
