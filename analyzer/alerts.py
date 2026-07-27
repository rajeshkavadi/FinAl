"""Alert evaluation for the monitor.

Given the current analysis, the suggestions, and the previous snapshot, emit a
list of Alerts. Alerts have a stable ``id`` so the monitor can apply a cooldown
and avoid re-firing the same condition every cycle. Two kinds of triggers:

  * level triggers  — a static condition is true now (concentration breach,
                      a live high-severity suggestion, LTCG eligibility);
  * change triggers — something moved vs the last snapshot (price/value move,
                      drawdown from peak, portfolio drop).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .analytics import Analysis, _asset_class
from .models import AssetType
from .state import MonitorState, Snapshot
from .tax import AssetClass, is_long_term

SEV_ORDER = {"high": 0, "warn": 1, "info": 2}


@dataclass
class Alert:
    id: str            # stable key for cooldown/dedupe
    severity: str      # high | warn | info
    category: str
    title: str
    message: str
    holding: str = ""
    value: float | None = None

    def to_dict(self) -> dict:
        return self.__dict__


@dataclass
class AlertConfig:
    single_stock: float = 0.20          # true/direct weight breach
    sector: float = 0.30
    holding_drop_pct: float = 0.10      # position down >=10% since last snapshot
    portfolio_drop_pct: float = 0.05    # whole book down >=5% since last snapshot
    drawdown_pct: float = 0.20          # position down >=20% from its peak
    price_move_pct: float = 0.08        # any single-day price move >=8%
    ltcg_window_days: int = 15          # alert when within N days of LTCG (or just crossed)
    surface_high_suggestions: bool = True
    cooldown_hours: float = 24.0


def _pct(x: float) -> str:
    return f"{x*100:.1f}%"


def evaluate(a: Analysis, sugs, state: MonitorState,
             config: AlertConfig | None = None,
             asof: date | None = None) -> list[Alert]:
    cfg = config or AlertConfig()
    asof = asof or date.today()
    prev: Snapshot | None = state.last_snapshot
    out: list[Alert] = []

    # ---- level: concentration (use look-through if available) ----------
    if a.lookthrough:
        for e in a.lookthrough.top(20):
            if e.pct >= cfg.single_stock:
                out.append(Alert(
                    id=f"concentration:{e.name}", severity="high",
                    category="concentration",
                    title=f"{e.name} is {_pct(e.pct)} true exposure",
                    message=(f"{e.name} look-through weight {_pct(e.pct)} "
                             f"(direct {_pct(e.direct_pct)}) is above the "
                             f"{_pct(cfg.single_stock)} limit."),
                    holding=e.name, value=e.pct))
    else:
        for c in a.by_stock:
            if c.pct >= cfg.single_stock:
                out.append(Alert(
                    id=f"concentration:{c.label}", severity="high",
                    category="concentration",
                    title=f"{c.label} is {_pct(c.pct)} of equity",
                    message=(f"{c.label} is {_pct(c.pct)} (₹{c.value:,.0f}), above "
                             f"the {_pct(cfg.single_stock)} single-stock limit."),
                    holding=c.label, value=c.pct))

    for c in a.by_theme:
        if c.pct >= cfg.sector and c.label != "Other / Unbucketed":
            out.append(Alert(
                id=f"sector:{c.label}", severity="warn", category="sector",
                title=f"{c.label} is {_pct(c.pct)} of equity",
                message=(f"{c.label} exposure {_pct(c.pct)} is above the "
                         f"{_pct(cfg.sector)} limit."),
                holding=c.label, value=c.pct))

    # ---- change: moves vs previous snapshot ----------------------------
    if prev:
        for h in a.pf.holdings:
            p = prev.holdings.get(h.name)
            if not p:
                out.append(Alert(
                    id=f"new_holding:{h.name}", severity="info", category="holding",
                    title=f"New holding detected: {h.name}",
                    message=f"{h.name} appeared since the last snapshot.",
                    holding=h.name))
                continue
            prev_val = p.get("value") or 0.0
            if prev_val > 0:
                chg = (h.current_value - prev_val) / prev_val
                if chg <= -cfg.holding_drop_pct:
                    out.append(Alert(
                        id=f"drop:{h.name}", severity="warn", category="move",
                        title=f"{h.name} down {_pct(abs(chg))} since last check",
                        message=(f"{h.name} fell from ₹{prev_val:,.0f} to "
                                 f"₹{h.current_value:,.0f} ({_pct(chg)})."),
                        holding=h.name, value=chg))
            # single-day price move
            pp, cp = p.get("price"), h.price
            if pp and cp and pp > 0:
                mv = (cp - pp) / pp
                if abs(mv) >= cfg.price_move_pct:
                    out.append(Alert(
                        id=f"price_move:{h.name}", severity="info", category="move",
                        title=f"{h.name} price moved {_pct(mv)}",
                        message=f"{h.name} price {pp:g} → {cp:g} ({_pct(mv)}).",
                        holding=h.name, value=mv))

        pv = prev.total_value or 0.0
        cv = sum(x.current_value for x in a.pf.holdings)
        if pv > 0 and (cv - pv) / pv <= -cfg.portfolio_drop_pct:
            out.append(Alert(
                id="portfolio_drop", severity="high", category="move",
                title=f"Portfolio down {_pct(abs((cv-pv)/pv))} since last check",
                message=f"Total value ₹{pv:,.0f} → ₹{cv:,.0f}.",
                value=(cv - pv) / pv))

    # ---- change: drawdown from peak ------------------------------------
    for h in a.pf.holdings:
        peak = state.peaks.get(h.name, 0.0)
        if peak > 0:
            dd = (h.current_value - peak) / peak
            if dd <= -cfg.drawdown_pct:
                out.append(Alert(
                    id=f"drawdown:{h.name}", severity="warn", category="drawdown",
                    title=f"{h.name} down {_pct(abs(dd))} from its peak",
                    message=(f"{h.name} is ₹{h.current_value:,.0f} vs a peak of "
                             f"₹{peak:,.0f} ({_pct(dd)})."),
                    holding=h.name, value=dd))

    # ---- level: LTCG eligibility crossing ------------------------------
    from .tax import _add_months
    for h in a.pf.holdings:
        if not h.buy_date or not h.valued_on_market:
            continue
        lt_months = 12 if _asset_class(h) == AssetClass.EQUITY else 24
        cross = _add_months(h.buy_date, lt_months)
        days_to = (cross - asof).days
        gain = (h.unrealised_pl or 0.0)
        if -cfg.ltcg_window_days <= days_to <= cfg.ltcg_window_days and gain > 0:
            when = ("just became" if days_to <= 0 else f"is {days_to}d from becoming")
            out.append(Alert(
                id=f"ltcg:{h.name}", severity="info", category="tax",
                title=f"{h.name} {when} long-term",
                message=(f"{h.name} {when} LTCG-eligible on {cross.isoformat()}; "
                         f"selling after that taxes gains at 12.5% (vs 20% STCG)."),
                holding=h.name, value=float(days_to)))

    # ---- level: surface live high-severity suggestions -----------------
    if cfg.surface_high_suggestions:
        for s in sugs:
            if s.severity == "high":
                out.append(Alert(
                    id=f"suggestion:{s.rule}:{','.join(s.impacted[:3])}",
                    severity="high", category="suggestion",
                    title=s.title, message=s.detail,
                    holding=", ".join(s.impacted[:3])))

    out.sort(key=lambda al: SEV_ORDER.get(al.severity, 3))
    return out


def apply_cooldown(alerts: list[Alert], state: MonitorState,
                   cooldown_hours: float, asof=None) -> list[Alert]:
    """Drop alerts still within their cooldown; mark the rest as fired."""
    fresh = []
    for al in alerts:
        if state.in_cooldown(al.id, cooldown_hours, asof):
            continue
        state.mark_fired(al.id, asof)
        fresh.append(al)
    return fresh
