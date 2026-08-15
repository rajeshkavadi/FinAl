"""Core data model for holdings.

Everything is intentionally lightweight (dataclasses) so the analyzer has no
hard dependency on pandas. Amounts are in the account currency (INR by default).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date as _date
from enum import Enum
from typing import Optional


class AssetType(str, Enum):
    EQUITY = "equity"          # direct stock / ETF held in demat
    MUTUAL_FUND = "mutual_fund"  # MF holding (lumpsum or SIP-accumulated)
    SIP = "sip"                # a recurring SIP *plan* (a cash flow, not a corpus)


# Ordered from lowest to highest so we can compare / aggregate.
RISK_ORDER = ["Low", "Medium", "Medium-High", "High", "Very High"]


def risk_rank(flag: Optional[str]) -> int:
    if not flag:
        return -1
    f = flag.strip()
    return RISK_ORDER.index(f) if f in RISK_ORDER else -1


@dataclass
class Holding:
    """A single position.

    ``invested`` is the cost basis / amount deployed. ``price`` is the latest
    known unit price (from the import or a live provider). ``quantity`` is
    optional: when present together with a live price we can compute a true
    current value; otherwise the analyzer falls back to ``invested`` (cost
    basis), which is what a units-less tracker gives us.
    """
    name: str
    asset_type: AssetType
    invested: float
    sector: str = "Uncategorised"
    category: str = ""            # MF category (Flexi Cap, Small Cap, ...)
    price: Optional[float] = None
    quantity: Optional[float] = None
    avg_cost: Optional[float] = None   # average buy price per unit
    buy_date: Optional[_date] = None   # earliest acquisition date (for CAGR/XIRR)
    sip_start: Optional[_date] = None  # for SIP-accumulated XIRR
    risk_flag: Optional[str] = None
    verdict: Optional[str] = None   # Hold / Review / Strong Hold / ...
    notes: str = ""
    isin: Optional[str] = None
    symbol: Optional[str] = None    # ticker for live equity price lookups
    price_source: Optional[str] = None  # where the live price came from (NSE/BSE/Screener/…)
    scheme_code: Optional[str] = None  # AMFI code for live NAV lookups
    # SIP-specific
    monthly_sip: Optional[float] = None
    units_estimated: bool = False   # units simulated from SIP history, not given

    def __post_init__(self) -> None:
        # Reconcile the invested / quantity / avg_cost triangle: given any two,
        # derive the third so downstream P/L is consistent.
        if self.invested in (None, 0) and self.quantity and self.avg_cost:
            self.invested = float(self.quantity) * float(self.avg_cost)
        elif self.avg_cost in (None, 0) and self.quantity:
            try:
                self.avg_cost = float(self.invested) / float(self.quantity)
            except (ZeroDivisionError, TypeError):
                pass

    # ---- derived -------------------------------------------------------
    @property
    def current_value(self) -> float:
        """Best available estimate of what the position is worth today.

        Uses price*quantity when both are known, otherwise the invested/cost
        amount. Kept explicit so the UI can say which basis was used.
        """
        if self.price is not None and self.quantity is not None:
            return float(self.price) * float(self.quantity)
        return float(self.invested)

    @property
    def valued_on_market(self) -> bool:
        return self.price is not None and self.quantity is not None

    @property
    def unrealised_pl(self) -> Optional[float]:
        if self.valued_on_market:
            return self.current_value - self.invested
        return None

    @property
    def return_pct(self) -> Optional[float]:
        """Absolute (not annualised) return on cost, as a fraction."""
        pl = self.unrealised_pl
        if pl is None or not self.invested:
            return None
        return pl / float(self.invested)

    @property
    def annualised_return(self) -> Optional[float]:
        """CAGR from ``buy_date`` to today, when a market value is known."""
        if not self.valued_on_market or not self.buy_date or not self.invested:
            return None
        from .returns import cagr
        return cagr(self.invested, self.current_value, self.buy_date)


@dataclass
class Portfolio:
    holdings: list[Holding] = field(default_factory=list)
    sips: list[Holding] = field(default_factory=list)  # SIP plans (cash flows)
    meta: dict = field(default_factory=dict)

    def equities(self) -> list[Holding]:
        return [h for h in self.holdings if h.asset_type == AssetType.EQUITY]

    def funds(self) -> list[Holding]:
        return [h for h in self.holdings if h.asset_type == AssetType.MUTUAL_FUND]

    @property
    def total_invested(self) -> float:
        return sum(h.invested for h in self.holdings)

    @property
    def total_value(self) -> float:
        return sum(h.current_value for h in self.holdings)

    @property
    def total_monthly_sip(self) -> float:
        return sum((h.monthly_sip or 0) for h in self.sips)

    # ---- P/L aggregates (only over market-valued holdings) --------------
    def market_valued(self) -> list["Holding"]:
        return [h for h in self.holdings if h.valued_on_market]

    @property
    def invested_market(self) -> float:
        """Cost basis of the holdings we can actually mark to market."""
        return sum(h.invested for h in self.market_valued())

    @property
    def value_market(self) -> float:
        return sum(h.current_value for h in self.market_valued())

    @property
    def total_unrealised_pl(self) -> Optional[float]:
        mv = self.market_valued()
        if not mv:
            return None
        return self.value_market - self.invested_market

    @property
    def total_return_pct(self) -> Optional[float]:
        pl = self.total_unrealised_pl
        if pl is None or not self.invested_market:
            return None
        return pl / self.invested_market

    @property
    def fully_valued(self) -> bool:
        """True when every holding can be marked to market (qty + price)."""
        return bool(self.holdings) and all(h.valued_on_market for h in self.holdings)
