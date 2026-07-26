"""Portfolio analytics: concentration, sector/risk exposure, overlap.

All figures are computed on the *current value* basis of each holding, which
falls back to invested/cost when quantities+live-price are unavailable. This
mirrors how a units-less tracker is analysed today, and upgrades automatically
once quantities are supplied.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from .models import AssetType, Holding, Portfolio, risk_rank

# Keyword buckets used to detect cross-holding thematic overlap. Extendable.
THEME_KEYWORDS = {
    "IT / Technology": ["it ", "it-", "software", "tech", "bpm", "bfsi", "digital"],
    "Financials / NBFC": ["nbfc", "bank", "financ", "microfinance", "mfi"],
    "Logistics": ["logistic", "cargo", "transport", "courier"],
    "Metals / Materials": ["metal", "recycl", "steel", "mining"],
    "Consumer": ["qsr", "consumer", "retail", "fmcg", "textile"],
    "Energy / Industrials": ["power", "epc", "ethanol", "engineering", "capital goods"],
}


def _bucket(sector: str) -> str | None:
    s = (sector or "").lower()
    for theme, kws in THEME_KEYWORDS.items():
        if any(k in s for k in kws):
            return theme
    return None


@dataclass
class Concentration:
    label: str
    value: float
    pct: float


def _breakdown(items: dict[str, float], total: float) -> list[Concentration]:
    out = [Concentration(k, v, (v / total if total else 0)) for k, v in items.items()]
    out.sort(key=lambda c: c.value, reverse=True)
    return out


def hhi(pcts: list[float]) -> float:
    """Herfindahl-Hirschman Index on fractional weights (0..1).

    ~1/N for an equal-weight book; ->1 for a single-name book. >0.25 is
    'concentrated' by the usual antitrust analogy (equivalent to < ~4 equal
    positions).
    """
    return sum(p * p for p in pcts)


def effective_positions(pcts: list[float]) -> float:
    h = hhi(pcts)
    return (1.0 / h) if h else 0.0


class Analysis:
    def __init__(self, pf: Portfolio):
        self.pf = pf
        eq = pf.equities()
        self.equity_total = sum(h.current_value for h in eq)

        # per-stock
        self.by_stock = _breakdown(
            {h.name: h.current_value for h in eq}, self.equity_total)

        # per-sector (as tagged)
        sec: dict[str, float] = defaultdict(float)
        for h in eq:
            sec[h.sector] += h.current_value
        self.by_sector = _breakdown(sec, self.equity_total)

        # thematic buckets (groups related sectors, e.g. all IT flavours)
        theme: dict[str, float] = defaultdict(float)
        for h in eq:
            b = _bucket(h.sector) or "Other / Unbucketed"
            theme[b] += h.current_value
        self.by_theme = _breakdown(theme, self.equity_total)

        # per-risk-flag
        risk: dict[str, float] = defaultdict(float)
        for h in eq:
            risk[h.risk_flag or "Unrated"] += h.current_value
        self.by_risk = _breakdown(risk, self.equity_total)

        self.high_risk_value = sum(
            h.current_value for h in eq if risk_rank(h.risk_flag) >= risk_rank("High"))
        self.high_risk_pct = (
            self.high_risk_value / self.equity_total if self.equity_total else 0)

        self.equity_hhi = hhi([c.pct for c in self.by_stock])
        self.equity_eff_positions = effective_positions([c.pct for c in self.by_stock])

        # SIP allocation by category
        sip_total = pf.total_monthly_sip
        cat: dict[str, float] = defaultdict(float)
        for s in pf.sips:
            cat[s.category or "Uncategorised"] += (s.monthly_sip or 0)
        self.sip_by_category = _breakdown(cat, sip_total)
        self.sip_total_monthly = sip_total

    # ---- overlap detection -------------------------------------------------
    def duplicate_positions(self) -> list[tuple[str, list[Holding]]]:
        """Same underlying held across multiple line items (e.g. averaging)."""
        groups: dict[str, list[Holding]] = defaultdict(list)
        for h in self.pf.equities():
            key = h.name.split("(")[0].strip().lower()
            groups[key].append(h)
        return [(k, v) for k, v in groups.items() if len(v) > 1]

    def category_overlaps(self) -> list[tuple[str, list[Holding]]]:
        """MF categories with more than one fund (candidate consolidation)."""
        groups: dict[str, list[Holding]] = defaultdict(list)
        for s in self.pf.sips:
            if s.category:
                groups[s.category].append(s)
        for h in self.pf.funds():
            if h.category:
                groups[h.category].append(h)
        return [(k, v) for k, v in groups.items() if len(v) > 1]
