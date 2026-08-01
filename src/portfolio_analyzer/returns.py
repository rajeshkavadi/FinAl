"""Return math: absolute return, CAGR, and XIRR over dated cashflows.

XIRR is the money-weighted annualised return that solves

    sum_i  cf_i / (1 + r) ** (days_i / 365) = 0

for r, where each cashflow ``cf_i`` is negative for money invested (outflow)
and positive for money received / current value (inflow). We solve with a
bracketed bisection (robust; no derivative needed) after a coarse scan.
"""
from __future__ import annotations

from datetime import date
from typing import Iterable, Optional

_DAYS = 365.0


def cagr(invested: float, current_value: float, start: date,
         end: Optional[date] = None) -> Optional[float]:
    """Compound annual growth rate for a single lumpsum."""
    end = end or date.today()
    years = (end - start).days / _DAYS
    if invested <= 0 or current_value <= 0 or years <= 0:
        return None
    return (current_value / invested) ** (1.0 / years) - 1.0


def _npv(rate: float, cashflows: list[tuple[date, float]], t0: date) -> float:
    total = 0.0
    for d, cf in cashflows:
        t = (d - t0).days / _DAYS
        total += cf / ((1.0 + rate) ** t)
    return total


def xirr(cashflows: Iterable[tuple[date, float]],
         low: float = -0.9999, high: float = 100.0) -> Optional[float]:
    """Solve for the annualised money-weighted return.

    ``cashflows`` is an iterable of (date, amount) with at least one negative
    (outflow) and one positive (inflow). Returns None if it can't bracket a
    root (e.g. all-positive or all-negative flows).
    """
    cfs = sorted(cashflows, key=lambda x: x[0])
    if len(cfs) < 2:
        return None
    if not (any(cf < 0 for _, cf in cfs) and any(cf > 0 for _, cf in cfs)):
        return None
    t0 = cfs[0][0]

    f_low, f_high = _npv(low, cfs, t0), _npv(high, cfs, t0)
    # Coarse scan to find a sign change if the initial bracket doesn't hold one.
    if f_low * f_high > 0:
        prev_r, prev_f = low, f_low
        found = False
        steps = 200
        for i in range(1, steps + 1):
            r = low + (high - low) * i / steps
            f = _npv(r, cfs, t0)
            if prev_f * f <= 0:
                low, high, f_low, f_high = prev_r, r, prev_f, f
                found = True
                break
            prev_r, prev_f = r, f
        if not found:
            return None

    for _ in range(200):
        mid = (low + high) / 2.0
        f_mid = _npv(mid, cfs, t0)
        if abs(f_mid) < 1e-7:
            return mid
        if f_low * f_mid < 0:
            high, f_high = mid, f_mid
        else:
            low, f_low = mid, f_mid
    return (low + high) / 2.0


def sip_cashflows(monthly: float, start: date, end: Optional[date] = None,
                  current_value: float = 0.0) -> list[tuple[date, float]]:
    """Synthesize monthly SIP outflows plus a terminal inflow of current value.

    Assumes a fixed monthly contribution on the same day-of-month from ``start``
    until ``end``. Good enough for an XIRR estimate when only the SIP amount,
    start date and present corpus are known.
    """
    end = end or date.today()
    flows: list[tuple[date, float]] = []
    y, m = start.year, start.month
    day = start.day
    while (y, m) <= (end.year, end.month):
        # clamp day to month length
        import calendar
        d = min(day, calendar.monthrange(y, m)[1])
        dt = date(y, m, d)
        if dt > end:
            break
        flows.append((dt, -abs(monthly)))
        m += 1
        if m > 12:
            m, y = 1, y + 1
    if current_value:
        flows.append((end, abs(current_value)))
    return flows
