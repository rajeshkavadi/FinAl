"""Live-ish fetcher for mutual-fund compositions.

Holdings come from the SEBI-mandated **monthly portfolio disclosure** every AMC
publishes — a spreadsheet listing each instrument with its "% to Net Assets".
This module:

  * parses those disclosure files (xlsx/csv) into a ``FundComposition``
    (``parse_disclosure``), keeping equity constituents and dropping debt /
    cash / TREPS / totals;
  * caches parsed compositions on disk with a freshness window
    (``CompositionCache``) — holdings change monthly, so refetching daily is
    wasteful;
  * can pull a disclosure from a configured URL (``HttpCompositionProvider``),
    with the network call isolated so everything else is testable offline;
  * exposes one orchestrator, ``fetch_compositions``, returning the same
    ``dict[str, FundComposition]`` that ``lookthrough.load_compositions`` does,
    so it drops straight into ``Analysis`` / the monitor.

No hard third-party dependency (openpyxl only if you parse .xlsx disclosures).
"""
from __future__ import annotations

import csv
import json
import re
import time
import urllib.request
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from .lookthrough import Constituent, FundComposition, norm

# Instrument names / section headers that are not equity constituents.
_SKIP = (
    "treps", "reverse repo", "repo", "net receivable", "net payable",
    "grand total", "total", "sub total", "subtotal", "cash", "clearing corp",
    "money market", "certificate of deposit", "commercial paper", "t-bill",
    "treasury bill", "government of india", "gsec", "g-sec", "sdl", "sovereign",
    "debt instrument", "bond", "debenture", "margin", "collateral",
    "equity & equity related", "listed / awaiting listing", "unlisted",
)


def _looks_like_debt_or_residual(name: str) -> bool:
    n = name.strip().lower()
    if not n:
        return True
    if re.match(r"^\d+(\.\d+)?\s*%", n):     # coupon-prefixed debt, e.g. "7.26% GOI 2033"
        return True
    return any(k in n for k in _SKIP)


def _is_debt_sector(sector: str) -> bool:
    """A '% Industry/Rating' value that denotes debt rather than an equity sector."""
    s = (sector or "").strip().lower()
    if not s:
        return False
    if "sovereign" in s or "rating" in s:
        return True
    # credit ratings like AAA, AA+, A1+, D
    return bool(re.match(r"^(a1\+?|aaa|aa|a|bbb|bb|b|c|d)[+-]?$", s))


def _to_pct(v) -> Optional[float]:
    """Parse a '% to Net Assets' cell into a fraction (e.g. 4.32 -> 0.0432)."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).strip().replace("%", "").replace(",", "")
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            return None
    # disclosures state this as a percentage; values are ~0..100
    if f > 1.5:            # clearly a percentage
        return f / 100.0
    return f / 100.0 if f > 0 else None   # small % like 0.5 -> 0.005


def _read_rows(path: str | Path) -> list[list]:
    p = Path(path)
    if p.suffix.lower() in (".xlsx", ".xlsm"):
        import openpyxl
        wb = openpyxl.load_workbook(p, data_only=True)
        ws = wb.active
        return [list(r) for r in ws.iter_rows(values_only=True)]
    with open(p, newline="", encoding="utf-8-sig") as fh:
        return [r for r in csv.reader(fh)]


def _find_header(rows: list[list]) -> tuple[int, dict[str, int]]:
    """Locate the disclosure header row and map the columns we need."""
    for i, row in enumerate(rows):
        low = [str(c or "").strip().lower() for c in row]
        joined = " | ".join(low)
        if "name of the instrument" in joined or (
                "instrument" in joined and "net asset" in joined):
            cols = {}
            for j, c in enumerate(low):
                if "name of the instrument" in c or c == "instrument" or c == "name":
                    cols["name"] = j
                elif "isin" in c:
                    cols["isin"] = j
                elif "industry" in c or "rating" in c:
                    cols["sector"] = j
                elif "net asset" in c or "% to nav" in c or c.strip() == "%":
                    cols["pct"] = j
            if "name" in cols and "pct" in cols:
                return i, cols
    raise ValueError("could not locate disclosure header "
                     "(need 'Name of the Instrument' and '% to Net Assets')")


def parse_disclosure(path: str | Path, fund_name: str | None = None) -> FundComposition:
    """Parse an AMC monthly portfolio disclosure into a FundComposition."""
    rows = _read_rows(path)
    hdr_i, cols = _find_header(rows)
    fund = fund_name or _guess_fund_name(rows[:hdr_i]) or Path(path).stem
    fc = FundComposition(fund=fund)
    for row in rows[hdr_i + 1:]:
        if cols["name"] >= len(row):
            continue
        name = str(row[cols["name"]] or "").strip()
        if _looks_like_debt_or_residual(name):
            continue
        pct = _to_pct(row[cols["pct"]]) if cols["pct"] < len(row) else None
        if not pct or pct <= 0:
            continue
        sector = ""
        if "sector" in cols and cols["sector"] < len(row):
            sector = str(row[cols["sector"]] or "").strip()
        if _is_debt_sector(sector):          # sovereign / credit-rating rows -> debt
            continue
        fc.holdings.append(Constituent(name=name, weight=pct, sector=sector))
    return fc


def _guess_fund_name(head_rows: list[list]) -> Optional[str]:
    for row in head_rows:
        for c in row:
            s = str(c or "").strip()
            if "fund" in s.lower() and len(s) > 6:
                return s
    return None


# --------------------------------------------------------------------------
class CompositionCache:
    """Disk cache of parsed compositions with a freshness window (days)."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._data: dict = {}
        if self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except Exception:
                self._data = {}

    def get(self, fund: str, max_age_days: float = 35) -> Optional[FundComposition]:
        rec = self._data.get(norm(fund))
        if not rec:
            return None
        age = (time.time() - rec.get("fetched_at", 0)) / 86400.0
        if age > max_age_days:
            return None
        return FundComposition(
            fund=rec["fund"],
            holdings=[Constituent(**c) for c in rec["holdings"]])

    def put(self, fc: FundComposition) -> None:
        self._data[norm(fc.fund)] = {
            "fund": fc.fund,
            "fetched_at": time.time(),
            "fetched_at_iso": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "holdings": [asdict(c) for c in fc.holdings],
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
class HttpCompositionProvider:
    """Fetch a disclosure file from a per-fund URL and parse it.

    ``url_template`` may contain ``{fund}`` (URL-encoded fund name). The
    downloaded bytes are written to a temp file and parsed by ``parse_disclosure``
    (csv/xlsx). Network is best-effort; failures return None so callers fall
    back to cache / disclosure files / manual data.
    """

    def __init__(self, url_template: str, timeout: int = 30):
        self.url_template = url_template
        self.timeout = timeout

    def fetch(self, fund: str) -> Optional[FundComposition]:
        import tempfile
        import urllib.parse
        url = self.url_template.replace("{fund}", urllib.parse.quote(fund))
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0 (portfolio-analyzer)"})
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                data = r.read()
        except Exception:
            return None
        suffix = ".xlsx" if url.lower().endswith((".xlsx", ".xlsm")) else ".csv"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tf:
            tf.write(data)
            tmp = tf.name
        try:
            return parse_disclosure(tmp, fund_name=fund)
        except Exception:
            return None
        finally:
            Path(tmp).unlink(missing_ok=True)


# --------------------------------------------------------------------------
def fetch_compositions(fund_names: Iterable[str], *,
                       disclosures: Optional[dict[str, str]] = None,
                       provider: Optional[HttpCompositionProvider] = None,
                       cache: Optional[CompositionCache] = None,
                       max_age_days: float = 35) -> dict[str, FundComposition]:
    """Resolve compositions for ``fund_names`` from cache, local disclosure
    files, then an HTTP provider — whichever answers first. Returns the dict
    keyed by ``norm(fund)`` that look-through expects.

    ``disclosures`` maps a fund name to a local disclosure file path.
    """
    out: dict[str, FundComposition] = {}
    disclosures = disclosures or {}
    disc_norm = {norm(k): v for k, v in disclosures.items()}
    for fund in fund_names:
        fc: Optional[FundComposition] = None
        if cache:
            fc = cache.get(fund, max_age_days)
        if fc is None and norm(fund) in disc_norm:
            try:
                fc = parse_disclosure(disc_norm[norm(fund)], fund_name=fund)
            except Exception:
                fc = None
        if fc is None and provider:
            fc = provider.fetch(fund)
        if fc is not None:
            out[norm(fund)] = fc
            if cache:
                cache.put(fc)
    if cache:
        cache.save()
    return out


def discover_disclosures(folder: str | Path) -> dict[str, str]:
    """Map fund name -> file path for every disclosure in a folder.

    The fund name is taken from the file inside (falling back to the file
    stem), so files can be named however the AMC exports them.
    """
    out: dict[str, str] = {}
    for p in Path(folder).glob("*"):
        if p.suffix.lower() not in (".csv", ".xlsx", ".xlsm"):
            continue
        try:
            fc = parse_disclosure(p)
            out[fc.fund] = str(p)
        except Exception:
            continue
    return out
