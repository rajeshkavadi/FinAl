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


_DEBT_HINTS = ("debt", "liquid", "gilt", "bond", "money market", "overnight",
               "corporate bond", "banking & psu", "ultra short", "low duration")


def _asset_class(h):
    """Map a holding to a tax asset class (equity vs debt MF vs other)."""
    from .models import AssetType
    from .tax import AssetClass
    if h.asset_type == AssetType.MUTUAL_FUND:
        blob = f"{h.category} {h.name}".lower()
        if any(k in blob for k in _DEBT_HINTS):
            return AssetClass.DEBT
        return AssetClass.EQUITY   # equity-oriented MF (STT) by default
    return AssetClass.EQUITY       # listed shares / ETF


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


@dataclass
class PLRow:
    name: str
    invested: float
    current_value: float
    pl: float
    return_pct: float
    annualised: float | None = None


@dataclass
class PLSummary:
    rows: list["PLRow"]
    invested: float
    current_value: float
    total_pl: float | None
    total_return_pct: float | None
    coverage: float            # fraction of holdings that could be marked to market
    winners: list["PLRow"]
    losers: list["PLRow"]

    @property
    def has_data(self) -> bool:
        return bool(self.rows)


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
    def __init__(self, pf: Portfolio, compositions=None):
        self.pf = pf
        eq = pf.equities()
        self.equity_total = sum(h.current_value for h in eq)

        # optional MF<->equity look-through (needs fund compositions + values)
        self.lookthrough = None
        if compositions and pf.funds():
            from .lookthrough import look_through
            self.lookthrough = look_through(pf, compositions)

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

        # ---- P/L summary (over market-valued holdings only) ------------
        self.pl = self._profit_and_loss()

    def _profit_and_loss(self) -> "PLSummary":
        mv = self.pf.market_valued()
        rows: list[PLRow] = []
        for h in mv:
            rows.append(PLRow(
                name=h.name,
                invested=h.invested,
                current_value=h.current_value,
                pl=h.unrealised_pl or 0.0,
                return_pct=h.return_pct or 0.0,
                annualised=h.annualised_return,
            ))
        rows.sort(key=lambda r: r.pl, reverse=True)
        invested = sum(r.invested for r in rows)
        value = sum(r.current_value for r in rows)
        return PLSummary(
            rows=rows,
            invested=invested,
            current_value=value,
            total_pl=(value - invested) if rows else None,
            total_return_pct=((value - invested) / invested) if invested else None,
            coverage=(len(mv) / len(self.pf.holdings)) if self.pf.holdings else 0.0,
            winners=[r for r in rows if r.pl > 0],
            losers=[r for r in rows if r.pl < 0],
        )

    def switch_tax(self, names, config=None, asof=None, slab_rate=None):
        """Estimate capital-gains tax on exiting the named holdings as a batch.

        Only holdings that can be marked to market (quantity + price) contribute
        a real gain; the rest are reported as needing quantities. Rates default
        to the regime in force on ``asof`` (today unless given). Returns a
        ``SwitchTaxReport`` (see ``tax.py``).
        """
        from .tax import AssetClass, TaxLine, estimate_switch_tax
        wanted = {n.strip().lower() for n in names}
        lines: list[TaxLine] = []
        missing_value: list[str] = []
        for h in self.pf.holdings:
            if h.name.strip().lower() not in wanted:
                continue
            if not h.valued_on_market:
                missing_value.append(h.name)
                continue
            lines.append(TaxLine(
                name=h.name,
                asset_class=_asset_class(h),
                invested=h.invested,
                current_value=h.current_value,
                buy_date=h.buy_date,
            ))
        rep = estimate_switch_tax(lines, config=config, asof=asof, slab_rate=slab_rate)
        if missing_value:
            rep.notes.append(
                "No quantity/price for: " + ", ".join(missing_value)
                + " — add them to include these in the tax estimate.")
        return rep

    def sell_candidates(self, sugs) -> list[str]:
        """Concrete stock names the suggestions imply trimming or exiting."""
        rules = {"single_stock_concentration", "averaging_into_speculative",
                 "micro_positions"}
        names: list[str] = []
        for s in sugs:
            if s.rule in rules:
                for n in s.impacted:
                    if n not in names:
                        names.append(n)
        return names

    def portfolio_xirr(self) -> float | None:
        """Money-weighted return across holdings/SIPs that carry dates.

        Lumpsum holdings contribute an outflow at ``buy_date`` and an inflow of
        current value today; SIPs contribute synthesized monthly outflows plus
        their current corpus. Returns None if no dated cashflows exist.
        """
        from datetime import date
        from .returns import sip_cashflows, xirr
        flows: list[tuple[date, float]] = []
        today = date.today()
        for h in self.pf.market_valued():
            if h.buy_date:
                flows.append((h.buy_date, -abs(h.invested)))
                flows.append((today, abs(h.current_value)))
        for s in self.pf.sips:
            if s.sip_start and s.monthly_sip and s.current_value:
                flows.extend(sip_cashflows(
                    s.monthly_sip, s.sip_start, today, s.current_value))
        return xirr(flows) if flows else None

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
