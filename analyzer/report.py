"""Render analysis + suggestions to a self-contained HTML dashboard."""
from __future__ import annotations

import datetime as _dt
import html

from .analytics import Analysis
from .models import Portfolio
from .rules import Suggestion

_SEV = {
    "high": ("#b42318", "#fef3f2", "High priority"),
    "warn": ("#b54708", "#fffaeb", "Review"),
    "info": ("#175cd3", "#eff8ff", "Housekeeping"),
}


def _rupees(x: float) -> str:
    return f"₹{x:,.0f}"


def _bars(rows, total, palette="#3b82f6") -> str:
    out = []
    mx = max((r.pct for r in rows), default=0) or 1
    for r in rows:
        w = r.pct / mx * 100
        out.append(f"""
        <div class="barrow">
          <div class="barlabel">{html.escape(r.label)}</div>
          <div class="bartrack"><div class="barfill" style="width:{w:.1f}%;background:{palette}"></div></div>
          <div class="barval">{_rupees(r.value)} · {r.pct*100:.1f}%</div>
        </div>""")
    return "".join(out)


def _signed(x: float) -> str:
    return f"{'+' if x >= 0 else '−'}₹{abs(x):,.0f}"


def _pl_color(x: float) -> str:
    return "#067647" if x >= 0 else "#b42318"


def _pl_section(a) -> str:
    pl = a.pl
    if not pl.has_data:
        return ('<div class="card"><p class="muted">No holdings could be marked to '
                'market yet. Add a <strong>Quantity</strong> column (and optionally '
                '<strong>Buy Date</strong>) to your import to unlock current value, '
                'profit/loss, and XIRR.</p></div>')

    xirr = a.portfolio_xirr()
    cov = pl.coverage
    kpis = [
        ("Invested", _rupees(pl.invested), "var(--ink)"),
        ("Current value", _rupees(pl.current_value), "var(--ink)"),
        ("Unrealised P/L", _signed(pl.total_pl or 0), _pl_color(pl.total_pl or 0)),
        ("Return", f"{(pl.total_return_pct or 0)*100:+.1f}%", _pl_color(pl.total_return_pct or 0)),
    ]
    if xirr is not None:
        kpis.append(("XIRR (money-weighted)", f"{xirr*100:+.1f}%", _pl_color(xirr)))
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpiv" style="color:{c}">{v}</div>'
        f'<div class="kpil">{k}</div></div>' for k, v, c in kpis)

    def _rows(rows):
        out = []
        for r in rows:
            ann = (f' · {r.annualised*100:+.1f}%/yr' if r.annualised is not None else "")
            out.append(f"""
            <div class="barrow plrow">
              <div class="barlabel">{html.escape(r.name)}</div>
              <div class="barval" style="color:{_pl_color(r.pl)}">{_signed(r.pl)}
                ({r.return_pct*100:+.1f}%{ann})</div>
            </div>""")
        return "".join(out) or '<p class="muted">—</p>'

    cov_note = ""
    if cov < 1.0:
        cov_note = (f'<p class="muted" style="margin-top:10px">P/L covers '
                    f'{cov*100:.0f}% of holdings (those with quantity + price). '
                    f'Add quantities for the rest to complete the picture.</p>')

    return f"""
    <div class="kpis" style="margin-bottom:6px">{kpi_html}</div>
    <div class="card">
      <div class="pltwo">
        <div><div class="plh">Top gainers</div>{_rows(pl.winners[:5])}</div>
        <div><div class="plh">Top losers</div>{_rows(pl.losers[:5])}</div>
      </div>
      {cov_note}
    </div>"""


def _suggestions_html(sugs: list[Suggestion]) -> str:
    if not sugs:
        return '<p class="muted">No rules triggered — the book is within all configured guidelines.</p>'
    cards = []
    for s in sugs:
        color, bg, badge = _SEV.get(s.severity, ("#475467", "#f2f4f7", "Note"))
        impacted = ""
        if s.impacted:
            chips = "".join(f'<span class="chip">{html.escape(i)}</span>' for i in s.impacted[:8])
            impacted = f'<div class="chips">{chips}</div>'
        cards.append(f"""
        <div class="sug" style="border-left:4px solid {color}">
          <div class="sughdr">
            <span class="badge" style="color:{color};background:{bg}">{badge}</span>
            <span class="sugtitle">{html.escape(s.title)}</span>
          </div>
          <p class="sugdetail">{html.escape(s.detail)}</p>
          {impacted}
          <p class="sugaction"><strong>Suggested:</strong> {html.escape(s.action)}</p>
        </div>""")
    return "".join(cards)


def build_dashboard(pf: Portfolio, a: Analysis, sugs: list[Suggestion],
                    *, title: str = "Portfolio Analysis") -> str:
    now = _dt.datetime.now().strftime("%d %b %Y, %H:%M")
    basis = ("live/market value" if any(h.valued_on_market for h in pf.equities())
             else "invested (cost) basis")

    kpis = [
        ("Direct equity", _rupees(a.equity_total)),
        ("Effective positions", f"{a.equity_eff_positions:.1f}"),
        ("High-risk share", f"{a.high_risk_pct*100:.1f}%"),
        ("Monthly SIP", _rupees(a.sip_total_monthly)),
    ]
    kpi_html = "".join(
        f'<div class="kpi"><div class="kpiv">{v}</div><div class="kpil">{k}</div></div>'
        for k, v in kpis)

    high_flags = sum(1 for s in sugs if s.severity == "high")
    warn_flags = sum(1 for s in sugs if s.severity == "warn")

    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
:root{{--bg:#f7f8fa;--card:#fff;--ink:#101828;--muted:#667085;--line:#eaecf0}}
*{{box-sizing:border-box}}
body{{margin:0;font:15px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink)}}
.wrap{{max-width:960px;margin:0 auto;padding:28px 20px 64px}}
h1{{font-size:24px;margin:0 0 2px}}
h2{{font-size:17px;margin:30px 0 12px}}
.muted{{color:var(--muted)}}
.sub{{color:var(--muted);font-size:13px;margin-bottom:20px}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px}}
@media(max-width:620px){{.kpis{{grid-template-columns:repeat(2,1fr)}}}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px}}
.kpiv{{font-size:22px;font-weight:650}}
.kpil{{color:var(--muted);font-size:12px;margin-top:2px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-top:12px}}
.barrow{{display:grid;grid-template-columns:150px 1fr 150px;gap:10px;align-items:center;margin:7px 0;font-size:13px}}
@media(max-width:620px){{.barrow{{grid-template-columns:110px 1fr;}}.barval{{grid-column:2}}}}
.barlabel{{color:var(--ink);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.bartrack{{background:#f2f4f7;border-radius:6px;height:16px;overflow:hidden}}
.barfill{{height:100%;border-radius:6px}}
.barval{{text-align:right;color:var(--muted);font-variant-numeric:tabular-nums}}
.sug{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:10px 0}}
.sughdr{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
.badge{{font-size:11px;font-weight:650;padding:3px 8px;border-radius:20px;text-transform:uppercase;letter-spacing:.03em}}
.sugtitle{{font-weight:600}}
.sugdetail{{margin:8px 0;color:#344054}}
.sugaction{{margin:8px 0 0;font-size:14px}}
.chips{{margin:6px 0}}
.chip{{display:inline-block;background:#f2f4f7;color:#344054;font-size:12px;padding:2px 8px;border-radius:6px;margin:2px 4px 2px 0}}
.pill{{display:inline-block;padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600}}
.pltwo{{display:grid;grid-template-columns:1fr 1fr;gap:24px}}
@media(max-width:620px){{.pltwo{{grid-template-columns:1fr}}}}
.plh{{font-size:13px;font-weight:650;color:var(--muted);margin-bottom:6px;text-transform:uppercase;letter-spacing:.03em}}
.plrow{{grid-template-columns:1fr auto}}
.disc{{margin-top:34px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
<h1>{html.escape(title)}</h1>
<div class="sub">Generated {now} · valued on {basis} ·
<span class="pill" style="background:#fef3f2;color:#b42318">{high_flags} high</span>
<span class="pill" style="background:#fffaeb;color:#b54708">{warn_flags} review</span></div>

<div class="kpis">{kpi_html}</div>

<h2>Profit &amp; loss</h2>
{_pl_section(a)}

<h2>Restructuring suggestions</h2>
{_suggestions_html(sugs)}

<h2>Single-stock concentration</h2>
<div class="card">{_bars(a.by_stock, a.equity_total, "#3b82f6")}</div>

<h2>Thematic exposure</h2>
<div class="card">{_bars(a.by_theme, a.equity_total, "#8b5cf6")}</div>

<h2>Risk-flag exposure</h2>
<div class="card">{_bars(a.by_risk, a.equity_total, "#ef4444")}</div>

<h2>SIP allocation by category</h2>
<div class="card">{_bars(a.sip_by_category, a.sip_total_monthly, "#10b981")}</div>

<p class="disc"><strong>Not investment advice.</strong> This dashboard is a
rule-based organisational tool built from data you provided. Figures use {basis};
verify all prices/quantities against your broker/demat statements. Capital-gains
tax on any switch is not computed here. Consult a SEBI-registered investment
adviser before acting.</p>
</div></body></html>"""
