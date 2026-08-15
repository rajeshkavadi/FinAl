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


def _tax_section(a, sugs) -> str:
    names = a.sell_candidates(sugs)
    if not names:
        return ('<div class="card"><p class="muted">No specific exit candidates '
                'flagged, so no switch-tax estimate is needed.</p></div>')
    rep = a.switch_tax(names)
    if not rep.lines:
        note = " ".join(rep.notes) if rep.notes else ""
        return ('<div class="card"><p class="muted">Tax on exiting the flagged '
                'positions can be estimated once they carry <strong>quantity, '
                'price and buy date</strong>. ' + html.escape(note) + '</p></div>')

    line_rows = []
    for ln in rep.lines:
        term_color = {"LTCG": "#067647", "STCG": "#b54708",
                      "UNKNOWN": "#b42318", "SLAB": "#175cd3"}.get(ln.term, "#475467")
        held = f"{ln.days_held}d" if ln.days_held is not None else "—"
        line_rows.append(f"""
        <tr><td>{html.escape(ln.name)}</td>
        <td class="num">{_signed(ln.gain)}</td>
        <td><span class="tterm" style="color:{term_color}">{ln.term}</span></td>
        <td class="num">{held}</td></tr>""")

    r = rep
    summary = [
        ("Gross proceeds (sell value)", _rupees(r.gross_proceeds)),
        ("Short-term gain → tax @20%", f"{_signed(r.stcg_gain)} → {_rupees(r.stcg_tax)}"),
        ("Long-term gain (after ₹1.25L exempt) → tax @12.5%",
         f"{_signed(r.ltcg_gain)} → {_rupees(r.ltcg_tax)}"),
        ("Estimated total tax", _rupees(r.total_tax)),
        ("Net proceeds after tax", _rupees(r.net_proceeds)),
        ("Tax drag on the switch", f"{r.tax_drag_pct*100:.1f}%"),
        ("Replacement must out-earn by", f"{r.breakeven_outperformance()*100:.1f}% just to break even"),
    ]
    summary_html = "".join(
        f'<div class="trow"><span>{html.escape(k)}</span><b>{v}</b></div>'
        for k, v in summary)
    notes = ""
    if r.notes:
        notes = "<ul class='tnotes'>" + "".join(
            f"<li>{html.escape(n)}</li>" for n in r.notes) + "</ul>"

    return f"""
    <div class="card">
      <p class="muted">If you exited the flagged exit-candidates this financial
      year, the estimated capital-gains cost is:</p>
      <table class="ttable">
        <thead><tr><th>Position</th><th class="num">Gain</th><th>Term</th><th class="num">Held</th></tr></thead>
        <tbody>{"".join(line_rows)}</tbody>
      </table>
      <div class="tsummary">{summary_html}</div>
      {notes}
      <p class="muted" style="font-size:12px;margin-top:10px">Includes 4% cess;
      excludes surcharge (income-dependent). Short/long-term losses are netted
      within their term and the ₹1.25L LTCG exemption is applied once to the
      batch. Estimate only — confirm with a tax professional.</p>
    </div>"""


def _lookthrough_section(a) -> str:
    lt = getattr(a, "lookthrough", None)
    if not lt:
        return ('<div class="card"><p class="muted">Add fund compositions '
                '(<code>--fund-holdings</code>) and fund values '
                '(<code>--funds</code>) to see your <strong>true</strong> exposure '
                'once your mutual funds are looked through into their underlying '
                'stocks.</p></div>')

    dfo = lt.direct_and_fund_overlaps()
    rows = []
    for e in lt.top(12):
        via = " · ".join(f"{k.split('(')[0].strip()} {v/lt.total_base*100:.1f}%"
                         for k, v in e.via_funds.items())
        badges = ""
        if e.direct > 0 and e.via_funds:
            badges = '<span class="lchip lred">direct + fund</span>'
        elif len(e.via_funds) >= 2:
            badges = '<span class="lchip lamb">multi-fund</span>'
        hidden = (f' <span class="muted">({e.hidden_multiplier:.1f}× direct)</span>'
                  if e.hidden_multiplier and e.hidden_multiplier >= 1.25 else "")
        rows.append(f"""
        <tr><td>{html.escape(e.name)} {badges}</td>
        <td class="num"><b>{e.pct*100:.1f}%</b>{hidden}</td>
        <td class="num">{e.direct_pct*100:.1f}%</td>
        <td class="lvia">{html.escape(via) or '—'}</td></tr>""")

    miss = ""
    if lt.funds_without_composition:
        miss = ('<p class="muted" style="font-size:12px;margin-top:8px">No composition '
                'supplied for: ' + html.escape(", ".join(lt.funds_without_composition))
                + ' — their value is treated as diversified/unattributed.</p>')
    unattr = (f'<p class="muted" style="font-size:12px">₹{lt.unattributed:,.0f} of fund '
              f'value is in un-named / residual holdings (counted in the base, not '
              f'attributed to a stock).</p>') if lt.unattributed else ""

    return f"""
    <div class="card">
      <p class="muted">True exposure once funds are looked through into their
      holdings, over a ₹{lt.total_base:,.0f} equity+MF base.
      <b>{len(dfo)}</b> name(s) appear both directly and inside your funds.</p>
      <table class="ttable">
        <thead><tr><th>Stock</th><th class="num">True %</th>
          <th class="num">Direct %</th><th>Via funds</th></tr></thead>
        <tbody>{"".join(rows)}</tbody>
      </table>
      {miss}{unattr}
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


def _download_button(name: str | None, b64: str | None) -> str:
    if not b64 or not name:
        return ""
    mime = ("data:application/vnd.openxmlformats-officedocument."
            "spreadsheetml.sheet;base64,")
    return (f"<a class='dlbtn' download='{html.escape(name)}' href='{mime}{b64}'>"
            f"⬇ Download your workbook with live prices filled in</a>")


def _px_source(h, live_names) -> str:
    if h.price is None:
        return "<span class='src cost'>no price</span>"
    if h.name in live_names:
        src = f" ({html.escape(h.price_source)})" if h.price_source else ""
        return f"<span class='src live'>live{src}</span>"
    return "<span class='src entered'>your price</span>"


def _cell_val(h):
    return _rupees(h.current_value) if h.valued_on_market else "—"


def _cell_pl(h):
    pl = h.unrealised_pl
    if pl is None:
        return "—"
    return f"<span style='color:{_pl_color(pl)}'>{_signed(pl)}</span>"


def _cell_ret(h):
    r = h.return_pct
    if r is None:
        return "—"
    return f"<span style='color:{_pl_color(r)}'>{r*100:+.1f}%</span>"


def _num_or_dash(x, fmt="{:,.2f}"):
    return fmt.format(x) if x is not None else "—"


def _stocks_table(pf: Portfolio, live_status: dict | None) -> str:
    eq = pf.equities()
    if not eq:
        return "<div class='card'><p class='muted'>No direct stocks in this upload.</p></div>"
    live_names = set((live_status or {}).get("equity_live", []))
    rows = "".join(
        f"<tr><td>{html.escape(h.name)}</td>"
        f"<td>{html.escape(h.symbol or '—')}</td>"
        f"<td class='num'>{_num_or_dash(h.quantity, '{:,.0f}')}</td>"
        f"<td class='num'>{_num_or_dash(h.avg_cost)}</td>"
        f"<td class='num'>{_num_or_dash(h.price)}</td>"
        f"<td>{_px_source(h, live_names)}</td>"
        f"<td class='num'>{_rupees(h.invested)}</td>"
        f"<td class='num'>{_cell_val(h)}</td>"
        f"<td class='num'>{_cell_pl(h)}</td>"
        f"<td class='num'>{_cell_ret(h)}</td></tr>"
        for h in sorted(eq, key=lambda x: x.current_value, reverse=True))
    return (
        "<div class='card' style='overflow-x:auto'>"
        "<table class='ttable'><thead><tr>"
        "<th>Stock</th><th>Symbol</th><th class='num'>Qty</th><th class='num'>Avg cost</th>"
        "<th class='num'>Price</th><th>Src</th><th class='num'>Invested</th>"
        "<th class='num'>Cur. value</th><th class='num'>P/L</th><th class='num'>Return</th>"
        f"</tr></thead><tbody>{rows}</tbody></table>"
        "<p class='muted' style='font-size:12px;margin:8px 0 0'>Current value = "
        "<b>Price × Qty</b> (computed here, not copied from your sheet). "
        "<span class='src live'>live</span> = fetched now · "
        "<span class='src entered'>your price</span> = the Current price you typed."
        "</p></div>")


def _funds_table(pf: Portfolio, live_status: dict | None) -> str:
    funds = pf.funds()
    if not funds:
        return "<div class='card'><p class='muted'>No mutual funds in this upload.</p></div>"
    spark = (live_status or {}).get("nav_spark", {})

    def _units_cell(h):
        if h.quantity is None:
            return "—"
        tag = " <span class='src entered'>est</span>" if h.units_estimated else ""
        return f"{h.quantity:,.2f}{tag}"

    def _nav_cell(h):
        nav = _num_or_dash(h.price, "{:,.4f}")
        sv = spark.get(h.name)
        if sv:
            return (f"{nav} <details class='spark'><summary>📈</summary>"
                    f"<div class='sparkbox'>{sv}<div class='sparkcap'>6-month NAV trend</div>"
                    f"</div></details>")
        return nav

    rows = "".join(
        f"<tr><td>{html.escape(h.name)}</td>"
        f"<td>{html.escape(h.category or '—')}</td>"
        f"<td class='num'>{_units_cell(h)}</td>"
        f"<td class='num'>{_nav_cell(h)}</td>"
        f"<td class='num'>{_rupees(h.invested) if h.invested else '—'}</td>"
        f"<td class='num'>{_cell_val(h)}</td>"
        f"<td class='num'>{_cell_pl(h)}</td></tr>"
        for h in funds)
    est_note = ""
    if (live_status or {}).get("units_estimated"):
        est_note = ("<b>est</b> = units estimated by simulating your monthly SIP "
                    "against NAV history (needs a <b>SIP Start</b> date; approximate). ")
    return (
        "<div class='card' style='overflow-x:auto'>"
        "<table class='ttable'><thead><tr>"
        "<th>Fund</th><th>Category</th><th class='num'>Units</th><th class='num'>NAV</th>"
        "<th class='num'>Invested</th><th class='num'>Cur. value</th><th class='num'>P/L</th>"
        "</tr></thead><tbody>{}</tbody></table>"
        "<p class='muted' style='font-size:12px;margin:8px 0 0'>{}Tap 📈 for the "
        "6-month NAV trend. Funds with no <b>Units</b> and no SIP start show NAV only — "
        "add Units (from your CAS) or a SIP Start date to value them.</p></div>"
        .format(rows, est_note))


def _holdings_tables(pf: Portfolio, live_status: dict | None) -> str:
    """Combined tables (kept for callers/tests that want both at once)."""
    out = []
    if pf.equities():
        out.append("<h2>Stocks — live price vs your cost</h2>"
                   + _stocks_table(pf, live_status))
    if pf.funds():
        out.append("<h2>Mutual funds — NAV &amp; value</h2>"
                   + _funds_table(pf, live_status))
    return "".join(out)


# tab whose content each suggestion rule belongs under
_FUND_SUGGESTION_RULES = {"mf_category_overlap"}


def _split_suggestions(sugs):
    fund = [s for s in sugs if s.rule in _FUND_SUGGESTION_RULES]
    stock = [s for s in sugs if s.rule not in _FUND_SUGGESTION_RULES]
    return stock, fund


def build_dashboard(pf: Portfolio, a: Analysis, sugs: list[Suggestion],
                    *, title: str = "Portfolio Analysis",
                    live_status: dict | None = None,
                    download_name: str | None = None,
                    download_b64: str | None = None) -> str:
    now = _dt.datetime.now().strftime("%d %b %Y, %H:%M")
    basis = ("live/market value" if any(h.valued_on_market for h in pf.holdings)
             else "invested (cost) basis")
    from . import __version__ as _ver

    kpis = [
        ("Direct equity", _rupees(a.equity_total),
         "Total current value of your direct stocks"),
        ("Holdings", f"{len(pf.equities())} stocks · {len(pf.funds())} funds",
         "How many direct stocks and mutual funds you hold"),
        ("High-risk share", f"{a.high_risk_pct*100:.1f}%",
         "Share of equity in holdings you flagged High / Very High risk"),
        ("Monthly SIP", _rupees(a.sip_total_monthly),
         "Total you invest via SIP each month"),
    ]
    kpi_html = "".join(
        f'<div class="kpi" title="{html.escape(tip)}"><div class="kpiv">{v}</div>'
        f'<div class="kpil">{k}</div></div>' for k, v, tip in kpis)

    _, fund_sugs = _split_suggestions(sugs)
    repl_sugs = [s for s in sugs if s.rule == "replacement_candidate"]
    shown = repl_sugs + fund_sugs
    high_flags = sum(1 for s in shown if s.severity == "high")
    warn_flags = sum(1 for s in shown if s.severity == "warn")

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
.ttable{{width:100%;border-collapse:collapse;font-size:13px;margin:4px 0 14px}}
.ttable th,.ttable td{{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}}
.ttable .num{{text-align:right;font-variant-numeric:tabular-nums}}
.tterm{{font-weight:650;font-size:12px}}
.tsummary{{background:#f9fafb;border:1px solid var(--line);border-radius:8px;padding:6px 12px}}
.trow{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px dashed var(--line);font-size:13px}}
.trow:last-child{{border-bottom:none}}
.tnotes{{margin:10px 0 0;padding-left:18px;color:var(--muted);font-size:12px}}
.tnotes li{{margin:3px 0}}
.lvia{{font-size:12px;color:var(--muted)}}
.lchip{{display:inline-block;font-size:10px;font-weight:650;padding:1px 6px;border-radius:5px;margin-left:6px;text-transform:uppercase;letter-spacing:.03em}}
.lred{{background:#fef3f2;color:#b42318}}
.lamb{{background:#fffaeb;color:#b54708}}
code{{background:#f2f4f7;padding:1px 5px;border-radius:4px;font-size:12px}}
.ver{{font-size:12px;font-weight:600;color:#fff;background:#3b82f6;border-radius:20px;padding:2px 9px;vertical-align:middle}}
.src{{font-size:10px;font-weight:700;padding:1px 6px;border-radius:5px;text-transform:uppercase;letter-spacing:.03em}}
.src.live{{background:#ecfdf3;color:#067647}}
.src.entered{{background:#fffaeb;color:#b54708}}
.src.cost{{background:#f2f4f7;color:#667085}}
.dlbtn{{display:inline-block;background:#067647;color:#fff;text-decoration:none;font-weight:600;font-size:13px;padding:9px 16px;border-radius:8px;margin:14px 0 0}}
.tabs{{margin-top:14px}}
.tabradio{{position:absolute;opacity:0;pointer-events:none}}
.tabbar{{display:flex;gap:4px;border-bottom:2px solid var(--line);margin:0 0 6px}}
.tabbar label{{padding:10px 20px;cursor:pointer;font-weight:650;font-size:15px;color:var(--muted);border-bottom:3px solid transparent;margin-bottom:-2px;user-select:none}}
.tabbar label:hover{{color:var(--ink)}}
#tab-stocks:checked ~ .tabbar label[for=tab-stocks],
#tab-funds:checked ~ .tabbar label[for=tab-funds]{{color:var(--ink);border-bottom-color:#3b82f6}}
.tabpane{{display:none}}
#tab-stocks:checked ~ #pane-stocks,
#tab-funds:checked ~ #pane-funds{{display:block}}
.tabpane h2:first-child{{margin-top:14px}}
.spark{{display:inline-block;margin-left:4px}}
.spark summary{{cursor:pointer;list-style:none}}
.spark summary::-webkit-details-marker{{display:none}}
.sparkbox{{position:absolute;background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px;margin-top:4px;box-shadow:0 6px 20px rgba(16,24,40,.12);z-index:5}}
.sparkcap{{font-size:10px;color:var(--muted);margin-top:2px;text-align:center}}
.disc{{margin-top:34px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:14px}}
</style></head><body><div class="wrap">
<h1>{html.escape(title)} <span class="ver">v{_ver}</span></h1>
<div class="sub">Generated {now} · valued on {basis} ·
<span class="pill" style="background:#fef3f2;color:#b42318">{high_flags} high</span>
<span class="pill" style="background:#fffaeb;color:#b54708">{warn_flags} review</span></div>
<div class="kpis">{kpi_html}</div>

<div class="tabs">
  <input type="radio" name="tab" id="tab-stocks" class="tabradio" checked>
  <input type="radio" name="tab" id="tab-funds" class="tabradio">
  <div class="tabbar">
    <label for="tab-stocks">📈 Stocks</label>
    <label for="tab-funds">💼 Mutual Funds</label>
  </div>

  <div class="tabpane" id="pane-stocks">
    <h2>Profit &amp; loss</h2>
    {_pl_section(a)}
    {_download_button(download_name, download_b64)}
    <h2>Holdings — live price vs your cost</h2>
    {_stocks_table(pf, live_status)}
    <h2>Replace with a better-fit equity</h2>
    <p class="muted" style="font-size:12px;margin:0 0 8px">Ranked by each holding's
    share of your equity. These flag positions worth swapping — the specific
    replacement is your call (see note below).</p>
    {_suggestions_html(repl_sugs)}
    <h2>Single-stock concentration</h2>
    <div class="card">{_bars(a.by_stock, a.equity_total, "#3b82f6")}</div>
    <h2>Thematic exposure</h2>
    <div class="card">{_bars(a.by_theme, a.equity_total, "#8b5cf6")}</div>
    <h2>Risk-flag exposure</h2>
    <div class="card">{_bars(a.by_risk, a.equity_total, "#ef4444")}</div>
    <h2>Tax impact of exiting flagged positions</h2>
    {_tax_section(a, sugs)}
  </div>

  <div class="tabpane" id="pane-funds">
    <h2>Your funds — NAV &amp; value</h2>
    {_funds_table(pf, live_status)}
    <h2>Suggestions &amp; considerations</h2>
    {_suggestions_html(fund_sugs)}
    <h2>Look-through exposure (funds + direct)</h2>
    {_lookthrough_section(a)}
    <h2>SIP allocation by category</h2>
    <div class="card">{_bars(a.sip_by_category, a.sip_total_monthly, "#10b981")}</div>
  </div>
</div>

<p class="disc"><strong>Not investment advice.</strong> This dashboard is a
rule-based organisational tool built from data you provided. Figures use {basis};
verify all prices/quantities against your broker/demat statements. Capital-gains
tax on any switch is not computed here. Consult a SEBI-registered investment
adviser before acting.</p>
</div></body></html>"""
