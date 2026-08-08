"""Capital-gains tax estimator for Indian listed equity and mutual funds.

Rules encoded (post 23-Jul-2024 Union Budget, applicable FY2024-25 onward):

  Listed equity shares & equity-oriented mutual funds (STT paid)
    - Short-term  (held <= 12 months): 20%      [Sec 111A]
    - Long-term   (held  > 12 months): 12.5% on aggregate LTCG above a
                                         ₹1,25,000 per-year exemption, no
                                         indexation                 [Sec 112A]

  Debt / non-equity mutual funds (units bought on/after 01-Apr-2023)
    - Taxed at the investor's slab rate regardless of holding period.

A 4% Health & Education cess is applied on the tax by default. Surcharge is
NOT modelled — it depends on total income and is investor-specific.

Everything here is an *estimate* to support decisions; it is not tax advice.
Actual liability depends on your full-year gains, set-offs, carry-forward
losses, surcharge, and residency. Confirm with a tax professional.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class AssetClass(str, Enum):
    EQUITY = "equity"   # listed shares + equity-oriented MF (STT paid)
    DEBT = "debt"       # debt / non-equity MF -> slab
    OTHER = "other"     # unlisted / other -> 24m LT threshold, 12.5%


# Budget 2024 cutover: the transfer/sale date decides which regime applies.
#   before 23-Jul-2024 : STCG 15% (111A), LTCG 10% (112A) over Rs 1,00,000
#   on/after 23-Jul-2024: STCG 20%,        LTCG 12.5%       over Rs 1,25,000
CG_REGIME_CUTOVER = date(2024, 7, 23)


@dataclass
class TaxConfig:
    equity_stcg_rate: float = 0.20
    equity_ltcg_rate: float = 0.125
    ltcg_exemption: float = 125000.0      # per financial year, aggregate
    equity_lt_months: int = 12
    other_lt_months: int = 24
    other_ltcg_rate: float = 0.125
    cess: float = 0.04                    # health & education cess on tax
    slab_rate: Optional[float] = None     # for DEBT/short-term OTHER; None=unknown

    @classmethod
    def for_date(cls, on: date, *, slab_rate: Optional[float] = None,
                 cess: float = 0.04) -> "TaxConfig":
        """Return the equity CG rates in force on ``on`` (the transfer date).

        Use this so a past-year computation gets that year's rates rather than
        the current ones. Defaults (a bare ``TaxConfig()``) stay on the current,
        post-Budget-2024 regime.
        """
        if on < CG_REGIME_CUTOVER:
            return cls(equity_stcg_rate=0.15, equity_ltcg_rate=0.10,
                       ltcg_exemption=100000.0, other_ltcg_rate=0.10,
                       cess=cess, slab_rate=slab_rate)
        return cls(equity_stcg_rate=0.20, equity_ltcg_rate=0.125,
                   ltcg_exemption=125000.0, other_ltcg_rate=0.125,
                   cess=cess, slab_rate=slab_rate)


def _add_months(d: date, months: int) -> date:
    m = d.month - 1 + months
    y = d.year + m // 12
    m = m % 12 + 1
    day = min(d.day, calendar.monthrange(y, m)[1])
    return date(y, m, day)


def is_long_term(buy_date: date, asof: date, lt_months: int) -> bool:
    """True if the asset is held for *more than* ``lt_months`` months."""
    return asof > _add_months(buy_date, lt_months)


@dataclass
class TaxLine:
    name: str
    asset_class: AssetClass
    invested: float
    current_value: float
    buy_date: Optional[date]
    gain: float = 0.0
    term: str = "UNKNOWN"        # STCG | LTCG | UNKNOWN
    days_held: Optional[int] = None
    reason: str = ""             # e.g. why term is UNKNOWN

    def __post_init__(self):
        self.gain = self.current_value - self.invested


@dataclass
class SwitchTaxReport:
    lines: list[TaxLine]
    config: TaxConfig
    asof: date
    stcg_gain: float = 0.0
    ltcg_gain: float = 0.0
    debt_gain: float = 0.0
    stcg_tax: float = 0.0
    ltcg_tax: float = 0.0
    ltcg_taxable_after_exemption: float = 0.0
    debt_tax: float = 0.0
    total_tax: float = 0.0
    gross_proceeds: float = 0.0
    net_proceeds: float = 0.0
    needs_buy_date: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def effective_rate(self) -> float:
        total_gain = self.stcg_gain + self.ltcg_gain + self.debt_gain
        return (self.total_tax / total_gain) if total_gain > 0 else 0.0

    @property
    def tax_drag_pct(self) -> float:
        """Tax as a fraction of the money you free up by selling."""
        return (self.total_tax / self.gross_proceeds) if self.gross_proceeds else 0.0

    def breakeven_outperformance(self) -> float:
        """How much more (as a fraction of net proceeds) the replacement must
        return just to recover the tax paid on the switch."""
        return (self.total_tax / self.net_proceeds) if self.net_proceeds else 0.0


def estimate_switch_tax(lines: list[TaxLine], config: TaxConfig | None = None,
                        asof: Optional[date] = None,
                        slab_rate: Optional[float] = None) -> SwitchTaxReport:
    """Estimate tax on selling ``lines`` as a batch in one financial year.

    Gains are netted *within* each term (short-term losses offset short-term
    gains, etc.), the LTCG exemption is applied once to the aggregate long-term
    gain, and cess is added on top. This mirrors how the batch would actually
    be assessed for the year.

    When ``config`` is omitted the equity rates are resolved from ``asof`` (the
    transfer date) via ``TaxConfig.for_date`` — so a past-year computation uses
    that year's rates, and the current year uses the post-Budget-2024 regime.
    Pass an explicit ``config`` to override. ``slab_rate`` applies to debt gains.
    """
    asof = asof or date.today()
    cfg = config or TaxConfig.for_date(asof, slab_rate=slab_rate)
    if config is not None and slab_rate is not None:
        cfg.slab_rate = slab_rate
    rep = SwitchTaxReport(lines=lines, config=cfg, asof=asof)

    for ln in lines:
        rep.gross_proceeds += ln.current_value
        if ln.buy_date:
            ln.days_held = (asof - ln.buy_date).days

        if ln.asset_class == AssetClass.DEBT:
            ln.term = "SLAB"
            rep.debt_gain += ln.gain
            continue

        lt_months = (cfg.equity_lt_months if ln.asset_class == AssetClass.EQUITY
                     else cfg.other_lt_months)
        if not ln.buy_date:
            ln.term = "UNKNOWN"
            ln.reason = "no buy date — cannot classify short vs long term"
            rep.needs_buy_date.append(ln.name)
            # conservatively treat as short-term so tax is not understated
            rep.stcg_gain += ln.gain
            continue

        if is_long_term(ln.buy_date, asof, lt_months):
            ln.term = "LTCG"
            rep.ltcg_gain += ln.gain
        else:
            ln.term = "STCG"
            rep.stcg_gain += ln.gain

    cess = 1.0 + cfg.cess

    # Short-term (equity 111A). Net losses reduce the taxable base to >=0.
    stcg_taxable = max(0.0, rep.stcg_gain)
    rep.stcg_tax = stcg_taxable * cfg.equity_stcg_rate * cess

    # Long-term (equity 112A) with the annual exemption on the aggregate.
    rep.ltcg_taxable_after_exemption = max(0.0, rep.ltcg_gain - cfg.ltcg_exemption)
    rep.ltcg_tax = rep.ltcg_taxable_after_exemption * cfg.equity_ltcg_rate * cess

    # Debt / non-equity at slab (if known).
    if rep.debt_gain > 0:
        if cfg.slab_rate is not None:
            rep.debt_tax = rep.debt_gain * cfg.slab_rate * cess
        else:
            rep.notes.append(
                f"₹{rep.debt_gain:,.0f} of debt/non-equity gain is taxed at your "
                "income-slab rate — set slab_rate to include it.")

    rep.total_tax = rep.stcg_tax + rep.ltcg_tax + rep.debt_tax
    rep.net_proceeds = rep.gross_proceeds - rep.total_tax

    if rep.ltcg_gain > 0 and rep.ltcg_gain <= cfg.ltcg_exemption:
        rep.notes.append(
            f"Long-term gain of ₹{rep.ltcg_gain:,.0f} is within the "
            f"₹{cfg.ltcg_exemption:,.0f} annual exemption — no LTCG tax if you "
            "have no other long-term gains this year.")
    if rep.needs_buy_date:
        rep.notes.append(
            "Positions without a buy date were treated as short-term "
            "(conservative); add buy dates for an accurate split.")
    return rep
