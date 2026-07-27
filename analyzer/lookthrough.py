"""MF <-> equity look-through overlap.

A mutual fund is a basket of underlying stocks. If you also hold some of those
stocks directly, your *true* exposure to them is higher than either your direct
book or your fund list shows on its own. This module "looks through" each fund
into its constituents and combines them with your direct equity to surface the
real, aggregated exposure — and the overlaps that create hidden concentration.

Fund composition (which stocks, at what weight) comes from factsheets / monthly
portfolio disclosures. Supply it as a CSV/JSON via ``load_compositions``; a live
fetcher can be slotted in later behind the same interface.
"""
from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models import AssetType, Portfolio

# Rows that are not a named stock (residual / cash-like) — excluded from
# stock-level attribution but still counted in the denominator.
_RESIDUAL = {"others", "other", "cash", "net current assets", "net receivables",
             "tri-party repo", "treps", "reverse repo", "cash & equivalents",
             "cash and equivalents", "cblo", "t-bills", "others & cash"}

_SUFFIXES = (" ltd.", " ltd", " limited", " (india)", " india", " corporation",
             " corp.", " corp", " co.", " company", " plc", " & co")


def norm(name: str) -> str:
    """Normalise a company name for matching across sources."""
    s = (name or "").strip().lower()
    for suf in _SUFFIXES:
        if s.endswith(suf):
            s = s[: -len(suf)]
    s = re.sub(r"[^a-z0-9]+", "", s)   # drop spaces/punct
    return s


@dataclass
class Constituent:
    name: str
    weight: float          # fraction of the fund (0..1)
    sector: str = ""


@dataclass
class FundComposition:
    fund: str
    holdings: list[Constituent] = field(default_factory=list)

    @property
    def named_weight(self) -> float:
        return sum(c.weight for c in self.holdings)


# --------------------------------------------------------------------------
def load_compositions(path: str | Path) -> dict[str, FundComposition]:
    """Load fund compositions from CSV or JSON.

    CSV columns (fuzzy, case-insensitive): Fund, Stock, Weight[, Sector].
    Weight may be a fraction (0.043) or a percentage (4.3). JSON form:
      {"Fund Name": [{"stock": "...", "weight": 4.3, "sector": "..."}, ...]}
    """
    p = Path(path)
    if p.suffix.lower() == ".json":
        raw = json.loads(p.read_text(encoding="utf-8"))
        comps: dict[str, FundComposition] = {}
        for fund, items in raw.items():
            fc = FundComposition(fund=fund)
            for it in items:
                fc.holdings.append(_mk_constituent(
                    it.get("stock") or it.get("name"),
                    it.get("weight"), it.get("sector", "")))
            comps[norm(fund)] = _clean(fc)
        return comps

    # CSV
    with open(p, newline="", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.reader(fh) if any(c.strip() for c in r)]
    if not rows:
        return {}
    hdr = [h.strip().lower() for h in rows[0]]

    def col(*needles):
        for n in needles:
            for i, h in enumerate(hdr):
                if n in h:
                    return i
        return None

    i_fund, i_stock = col("fund", "scheme"), col("stock", "company", "holding", "name")
    i_w, i_sec = col("weight", "%", "allocation"), col("sector", "industry")
    comps = {}
    for r in rows[1:]:
        fund = r[i_fund].strip() if i_fund is not None and i_fund < len(r) else ""
        stock = r[i_stock].strip() if i_stock is not None and i_stock < len(r) else ""
        if not fund or not stock:
            continue
        w = r[i_w] if i_w is not None and i_w < len(r) else None
        sec = r[i_sec] if i_sec is not None and i_sec < len(r) else ""
        key = norm(fund)
        comps.setdefault(key, FundComposition(fund=fund)).holdings.append(
            _mk_constituent(stock, w, sec))
    return {k: _clean(v) for k, v in comps.items()}


def _mk_constituent(stock, weight, sector) -> Constituent:
    try:
        w = float(str(weight).replace("%", "").strip()) if weight not in (None, "") else 0.0
    except ValueError:
        w = 0.0
    if w > 1.0:            # given as a percentage
        w /= 100.0
    return Constituent(name=str(stock).strip(), weight=w, sector=str(sector or "").strip())


def _clean(fc: FundComposition) -> FundComposition:
    fc.holdings = [c for c in fc.holdings if norm(c.name) not in
                   {norm(x) for x in _RESIDUAL} and c.weight > 0]
    return fc


# --------------------------------------------------------------------------
@dataclass
class Exposure:
    name: str
    sector: str
    total: float = 0.0
    direct: float = 0.0
    via_funds: dict[str, float] = field(default_factory=dict)
    pct: float = 0.0
    direct_pct: float = 0.0

    @property
    def sources(self) -> int:
        return (1 if self.direct > 0 else 0) + len(self.via_funds)

    @property
    def hidden_multiplier(self) -> Optional[float]:
        """How much bigger true exposure is vs the direct-only view."""
        if self.direct <= 0:
            return None
        return self.total / self.direct


@dataclass
class LookThroughResult:
    exposures: list[Exposure]
    total_base: float
    matched_fund_value: float
    unattributed: float          # fund value in residual/unnamed constituents
    funds_without_composition: list[str] = field(default_factory=list)

    def direct_and_fund_overlaps(self) -> list[Exposure]:
        return [e for e in self.exposures if e.direct > 0 and e.via_funds]

    def multi_fund_overlaps(self) -> list[Exposure]:
        return [e for e in self.exposures if len(e.via_funds) >= 2]

    def top(self, n: int = 12) -> list[Exposure]:
        return self.exposures[:n]


def _fund_value(h) -> float:
    return h.current_value


def look_through(pf: Portfolio,
                 compositions: dict[str, FundComposition]) -> LookThroughResult:
    """Combine direct equity with fund constituents into one exposure map."""
    exp: dict[str, Exposure] = {}

    def bump(name, sector) -> Exposure:
        k = norm(name)
        if k not in exp:
            exp[k] = Exposure(name=name, sector=sector or "Uncategorised")
        elif sector and exp[k].sector in ("", "Uncategorised"):
            exp[k].sector = sector
        return exp[k]

    direct_total = 0.0
    for h in pf.equities():
        e = bump(h.name, h.sector)
        e.direct += h.current_value
        e.total += h.current_value
        direct_total += h.current_value

    matched_fund_value = 0.0
    unattributed = 0.0
    missing: list[str] = []
    fund_total = 0.0
    for f in pf.funds():
        v = _fund_value(f)
        fund_total += v
        comp = compositions.get(norm(f.name))
        if not comp:
            missing.append(f.name)
            unattributed += v
            continue
        for c in comp.holdings:
            amt = v * c.weight
            e = bump(c.name, c.sector)
            e.via_funds[f.name] = e.via_funds.get(f.name, 0.0) + amt
            e.total += amt
            matched_fund_value += amt
        # weight not covered by named holdings stays diversified/unattributed
        unattributed += v * max(0.0, 1.0 - comp.named_weight)

    total_base = direct_total + fund_total
    for e in exp.values():
        e.pct = (e.total / total_base) if total_base else 0.0
        e.direct_pct = (e.direct / total_base) if total_base else 0.0

    rows = sorted(exp.values(), key=lambda e: e.total, reverse=True)
    return LookThroughResult(
        exposures=rows,
        total_base=total_base,
        matched_fund_value=matched_fund_value,
        unattributed=unattributed,
        funds_without_composition=missing,
    )
