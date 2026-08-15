"""Rule-based restructuring suggestion engine.

Each rule inspects the Analysis and emits zero or more Suggestions. Rules are
deliberately transparent and threshold-driven (no black box) so every flag can
be explained to the user. Thresholds live in ``DEFAULT_THRESHOLDS`` and can be
overridden per-run.

This engine is advisory only. It never places trades and always defers the
final decision (and tax computation) to the user / their SEBI-registered
advisor.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from .analytics import Analysis
from .models import risk_rank

Severity = str  # "info" | "warn" | "high"

DEFAULT_THRESHOLDS = {
    "single_stock_warn": 0.15,     # a single stock above 15% of equity
    "single_stock_high": 0.20,     # ...above 20% is a serious flag
    "sector_warn": 0.25,           # any theme above 25% of equity
    "high_risk_book": 0.40,        # High+VeryHigh names above 40% of equity
    "micro_position": 0.015,       # positions under 1.5% add tracking noise
    "min_effective_positions": 8,  # want at least ~8 effective equal bets
    "drawdown_review": 0.25,       # a position down >25% from cost warrants a review
}


@dataclass
class Suggestion:
    rule: str
    severity: Severity
    title: str
    detail: str
    impacted: list[str] = field(default_factory=list)
    action: str = ""

    def to_dict(self) -> dict:
        return self.__dict__


Rule = Callable[[Analysis, dict], list[Suggestion]]
_REGISTRY: list[Rule] = []


def rule(fn: Rule) -> Rule:
    _REGISTRY.append(fn)
    return fn


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# --------------------------------------------------------------------------
@rule
def single_stock_concentration(a: Analysis, t: dict) -> list[Suggestion]:
    out = []
    for c in a.by_stock:
        if c.pct >= t["single_stock_high"]:
            sev = "high"
        elif c.pct >= t["single_stock_warn"]:
            sev = "warn"
        else:
            continue
        out.append(Suggestion(
            rule="single_stock_concentration",
            severity=sev,
            title=f"{c.label} is {_pct(c.pct)} of direct equity",
            detail=(f"A single position of {_pct(c.pct)} (₹{c.value:,.0f}) exceeds "
                    f"the {_pct(t['single_stock_high'] if sev=='high' else t['single_stock_warn'])} "
                    f"guideline. Single-name blow-up risk is elevated."),
            impacted=[c.label],
            action=f"Consider trimming {c.label} toward a 10–12% cap and "
                   f"redeploying into under-weight areas.",
        ))
    return out


@rule
def sector_concentration(a: Analysis, t: dict) -> list[Suggestion]:
    out = []
    for c in a.by_theme:
        if c.pct >= t["sector_warn"] and c.label != "Other / Unbucketed":
            out.append(Suggestion(
                rule="sector_concentration",
                severity="high" if c.pct >= t["sector_warn"] * 1.4 else "warn",
                title=f"{c.label} exposure is {_pct(c.pct)} of direct equity",
                detail=(f"₹{c.value:,.0f} sits in the {c.label} theme, above the "
                        f"{_pct(t['sector_warn'])} guideline. Swapping laggards for "
                        f"leaders inside the same theme does not reduce this."),
                impacted=[c.label],
                action=f"Cap {c.label} nearer {_pct(t['sector_warn'])}; reduce theme "
                       f"weight rather than only rotating names within it.",
            ))
    return out


@rule
def high_risk_book(a: Analysis, t: dict) -> list[Suggestion]:
    if a.high_risk_pct >= t["high_risk_book"]:
        names = [c.label for c in a.by_stock]  # informational; detail names below
        return [Suggestion(
            rule="high_risk_book",
            severity="high",
            title=f"{_pct(a.high_risk_pct)} of equity is in High / Very-High risk names",
            detail=(f"₹{a.high_risk_value:,.0f} carries a High or Very-High risk flag, "
                    f"above the {_pct(t['high_risk_book'])} guideline. The book behaves "
                    f"like a speculative satellite, not a diversified core."),
            impacted=names,
            action="Rebalance toward quality / larger-cap names or a core index sleeve "
                   "until high-risk exposure is nearer a third of the book.",
        )]
    return []


@rule
def diversification_breadth(a: Analysis, t: dict) -> list[Suggestion]:
    eff = a.equity_eff_positions
    if eff and eff < t["min_effective_positions"]:
        return [Suggestion(
            rule="diversification_breadth",
            severity="warn",
            title=f"Only ~{eff:.1f} effective positions (HHI {a.equity_hhi:.3f})",
            detail=(f"Although you hold {len(a.by_stock)} stocks, weighting makes it behave "
                    f"like ~{eff:.1f} equal bets. Concentration is doing the work, not "
                    f"breadth."),
            action="Even out position sizes so the effective count rises toward the "
                   f"{t['min_effective_positions']}+ range.",
        )]
    return []


@rule
def averaging_into_speculative(a: Analysis, t: dict) -> list[Suggestion]:
    out = []
    for key, group in a.duplicate_positions():
        total = sum(h.current_value for h in group)
        worst = max(risk_rank(h.risk_flag) for h in group)
        name = group[0].name.split("(")[0].strip()
        sev = "high" if worst >= risk_rank("High") else "info"
        out.append(Suggestion(
            rule="averaging_into_speculative",
            severity=sev,
            title=f"{name} held across {len(group)} tranches (₹{total:,.0f})",
            detail=("Multiple tranches in the same name concentrate conviction. "
                    + ("The risk flag here is elevated, so averaging down amplifies "
                       "single-name risk." if sev == "high" else
                       "Consolidate for cleaner tracking.")),
            impacted=[h.name for h in group],
            action=f"Re-evaluate the combined {name} thesis as one position rather "
                   "than averaging further.",
        ))
    return out


@rule
def mf_category_overlap(a: Analysis, t: dict) -> list[Suggestion]:
    out = []
    for cat, funds in a.category_overlaps():
        out.append(Suggestion(
            rule="mf_category_overlap",
            severity="warn",
            title=f"{len(funds)} funds overlap in '{cat}'",
            detail=("Multiple funds in the same category tend to hold similar "
                    "underlying stocks, adding cost and tracking overhead without "
                    "extra diversification."),
            impacted=[f.name for f in funds],
            action=f"Keep the higher-conviction fund in '{cat}' and redirect the "
                   "overlapping SIP elsewhere.",
        ))
    return out


@rule
def micro_positions(a: Analysis, t: dict) -> list[Suggestion]:
    tiny = [c for c in a.by_stock if c.pct < t["micro_position"]]
    if len(tiny) >= 2:
        names = [c.label for c in tiny]
        total = sum(c.value for c in tiny)
        return [Suggestion(
            rule="micro_positions",
            severity="info",
            title=f"{len(tiny)} sub-{_pct(t['micro_position'])} positions (₹{total:,.0f})",
            detail=("Very small positions rarely move the portfolio but each adds "
                    "monitoring and tax-lot overhead."),
            impacted=names,
            action="Consolidate these into existing high-conviction names to simplify "
                   "tracking.",
        )]
    return []


@rule
def lookthrough_hidden_concentration(a: Analysis, t: dict) -> list[Suggestion]:
    lt = getattr(a, "lookthrough", None)
    if not lt:
        return []
    out = []
    for e in lt.top(20):
        # a name whose *true* (direct + via-fund) weight breaches the guideline,
        # especially when the direct-only view understates it
        if e.pct >= t["single_stock_warn"]:
            sev = "high" if e.pct >= t["single_stock_high"] else "warn"
            hidden = ""
            if e.hidden_multiplier and e.hidden_multiplier >= 1.25 and e.via_funds:
                hidden = (f" Direct holdings show only {_pct(e.direct_pct)}, but your "
                          f"funds add more of the same name.")
            src = "direct + " + ", ".join(e.via_funds) if e.via_funds else "direct"
            out.append(Suggestion(
                rule="lookthrough_hidden_concentration",
                severity=sev,
                title=f"{e.name}: {_pct(e.pct)} true exposure (look-through)",
                detail=(f"Combining direct holdings and fund constituents, {e.name} is "
                        f"{_pct(e.pct)} (₹{e.total:,.0f}) of the equity+MF portfolio via "
                        f"{src}.{hidden}"),
                impacted=[e.name],
                action=f"Count fund holdings when sizing {e.name}; trim the direct leg "
                       "if the combined weight is above your comfort.",
            ))
    return out


@rule
def lookthrough_direct_fund_overlap(a: Analysis, t: dict) -> list[Suggestion]:
    lt = getattr(a, "lookthrough", None)
    if not lt:
        return []
    overlaps = [e for e in lt.direct_and_fund_overlaps() if e.pct < t["single_stock_warn"]]
    if not overlaps:
        return []
    overlaps.sort(key=lambda e: e.total, reverse=True)
    names = [e.name for e in overlaps[:10]]
    return [Suggestion(
        rule="lookthrough_direct_fund_overlap",
        severity="info",
        title=f"{len(overlaps)} stock(s) held both directly and inside your funds",
        detail=("These names sit in your direct book and inside funds you own, so your "
                "true exposure is larger than the direct positions suggest. Not a "
                "problem by itself, but worth counting when you add to them."),
        impacted=names,
        action="Treat direct + fund exposure as one number per name before topping up.",
    )]


@rule
def large_drawdown_review(a: Analysis, t: dict) -> list[Suggestion]:
    """Flag holdings deep underwater vs cost — a prompt to revisit the thesis
    (a 'consideration', not just a restructuring cut)."""
    out = []
    for h in a.pf.equities():
        r = h.return_pct
        if r is None or r > -t["drawdown_review"]:
            continue
        out.append(Suggestion(
            rule="large_drawdown_review",
            severity="high" if r <= -0.40 else "warn",
            title=f"{h.name} is down {r*100:.0f}% from your cost",
            detail=(f"Now ₹{h.current_value:,.0f} vs invested ₹{h.invested:,.0f} "
                    f"({h.unrealised_pl:+,.0f}). A drawdown this deep is worth a "
                    "deliberate call rather than holding by inertia."),
            impacted=[h.name],
            action=("Revisit the original thesis: add with conviction, hold, or exit "
                    "and redeploy — but choose, don't drift."),
        ))
    return out


@rule
def replacement_candidate(a: Analysis, t: dict) -> list[Suggestion]:
    """Share-weighted 'replace with an alternative equity' suggestions.

    Ranked by each holding's share of your direct equity: a position that is a
    meaningful slice of the book AND is either deep underwater, high-risk, or
    over-concentrated is flagged as a candidate to swap for a better-fit equity.
    We describe the *kind* of replacement (a category-leading large-cap or a
    broad-market index fund/ETF) rather than naming a specific stock — the pick
    is the user's / their adviser's call.
    """
    by_name = {h.name: h for h in a.pf.equities()}
    out = []
    for c in a.by_stock:                         # sorted by value (share) already
        h = by_name.get(c.label)
        if h is None:
            continue
        w = c.pct                                # share of direct equity
        r = h.return_pct
        high_risk = risk_rank(h.risk_flag) >= risk_rank("High")
        reasons = []
        if r is not None and r <= -0.20:
            reasons.append(f"down {r*100:.0f}% vs your cost")
        if high_risk and w >= 0.05:
            reasons.append(f"{h.risk_flag} risk at {_pct(w)} of equity")
        if w >= t["single_stock_high"]:
            reasons.append(f"over-concentrated at {_pct(w)}")
        if not reasons:
            continue
        sev = ("high" if (r is not None and r <= -0.35) or w >= 0.25
               or (high_risk and w >= 0.10) else "warn")
        out.append(Suggestion(
            rule="replacement_candidate",
            severity=sev,
            title=f"Replace / reduce {c.label} — {_pct(w)} of your equity",
            detail=(f"{c.label} is ₹{c.value:,.0f} ({_pct(w)} of direct equity); "
                    + "; ".join(reasons) + "."),
            impacted=[c.label],
            action=(f"Consider swapping {c.label} for a better-fit equity in the same "
                    "space — a category-leading large-cap or a broad-market index "
                    f"fund/ETF — redeploying about ₹{c.value:,.0f}."),
        ))
    return out


def run_rules(a: Analysis, thresholds: dict | None = None) -> list[Suggestion]:
    t = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    out: list[Suggestion] = []
    for r in _REGISTRY:
        out.extend(r(a, t))
    order = {"high": 0, "warn": 1, "info": 2}
    out.sort(key=lambda s: order.get(s.severity, 3))
    return out
