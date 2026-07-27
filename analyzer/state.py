"""Persistent state for the monitor: snapshots, peaks, and alert cooldowns.

A snapshot is a lightweight JSON record of the portfolio at a point in time so
the next run can diff against it (price/value moves, drawdown from peak, LTCG
crossings) and so the same alert isn't re-fired every cycle (cooldown).

Default location: ``~/.portfolio_monitor/state.json`` (override per call). No
external dependencies — plain JSON on disk.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

DEFAULT_PATH = Path.home() / ".portfolio_monitor" / "state.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Snapshot:
    ts: str                                   # ISO timestamp
    holdings: dict[str, dict]                 # name -> {value, price, invested}
    total_value: float = 0.0
    total_invested: float = 0.0

    @classmethod
    def from_portfolio(cls, pf) -> "Snapshot":
        h = {}
        for x in pf.holdings:
            h[x.name] = {
                "value": round(x.current_value, 2),
                "price": x.price,
                "invested": round(x.invested, 2),
            }
        return cls(
            ts=_now().isoformat(),
            holdings=h,
            total_value=round(sum(x.current_value for x in pf.holdings), 2),
            total_invested=round(sum(x.invested for x in pf.holdings), 2),
        )


@dataclass
class MonitorState:
    path: Path
    last_snapshot: Optional[Snapshot] = None
    peaks: dict[str, float] = field(default_factory=dict)   # name/'__portfolio__' -> peak value
    last_alert: dict[str, str] = field(default_factory=dict)  # alert_id -> ISO ts

    # ---- persistence ---------------------------------------------------
    @classmethod
    def load(cls, path: str | Path = DEFAULT_PATH) -> "MonitorState":
        p = Path(path)
        if not p.exists():
            return cls(path=p)
        raw = json.loads(p.read_text(encoding="utf-8"))
        snap = raw.get("last_snapshot")
        return cls(
            path=p,
            last_snapshot=Snapshot(**snap) if snap else None,
            peaks=raw.get("peaks", {}),
            last_alert=raw.get("last_alert", {}),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "last_snapshot": self.last_snapshot.__dict__ if self.last_snapshot else None,
            "peaks": self.peaks,
            "last_alert": self.last_alert,
        }
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # ---- helpers -------------------------------------------------------
    def update_peaks(self, pf) -> None:
        for x in pf.holdings:
            self.peaks[x.name] = max(self.peaks.get(x.name, 0.0), x.current_value)
        tv = sum(x.current_value for x in pf.holdings)
        self.peaks["__portfolio__"] = max(self.peaks.get("__portfolio__", 0.0), tv)

    def in_cooldown(self, alert_id: str, hours: float, asof: Optional[datetime] = None) -> bool:
        prev = self.last_alert.get(alert_id)
        if not prev:
            return False
        asof = asof or _now()
        try:
            last = datetime.fromisoformat(prev)
        except ValueError:
            return False
        return (asof - last).total_seconds() < hours * 3600.0

    def mark_fired(self, alert_id: str, asof: Optional[datetime] = None) -> None:
        self.last_alert[alert_id] = (asof or _now()).isoformat()
