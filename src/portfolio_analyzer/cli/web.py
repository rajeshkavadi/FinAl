#!/usr/bin/env python3
"""Local web UI over the analyzer — upload/sync in a browser, get the dashboard.

Stdlib only (http.server): no Flask, no build step. Binds to 127.0.0.1 by
default because it reads your financial files — keep it local.

    python webapp.py                 # then open http://127.0.0.1:8765
    python webapp.py --port 9000 --host 127.0.0.1

Upload any combination of: a portfolio file (xlsx/CSV), a broker holdings CSV,
a CAS statement, an MF-holdings CSV, and a fund-compositions file. The server
assembles them into one portfolio and returns the same self-contained dashboard
that run.py produces. Nothing is uploaded anywhere off your machine.
"""
from __future__ import annotations

import argparse
import html
import sys
import tempfile
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


from portfolio_analyzer import sample_path
from portfolio_analyzer.analytics import Analysis
from portfolio_analyzer.loader import load, load_csv
from portfolio_analyzer.models import AssetType, Portfolio
from portfolio_analyzer.report import build_dashboard
from portfolio_analyzer.rules import run_rules


# --------------------------------------------------------------------------
# multipart/form-data parsing (stdlib cgi is gone in 3.13, so parse by hand)
# --------------------------------------------------------------------------
def parse_multipart(body: bytes, boundary: bytes) -> dict[str, dict]:
    """Return {field_name: {'filename': str|None, 'content': bytes}}."""
    delim = b"--" + boundary
    out: dict[str, dict] = {}
    for part in body.split(delim):
        if part in (b"", b"--", b"--\r\n", b"\r\n"):
            continue
        if part.startswith(b"\r\n"):
            part = part[2:]
        if part.endswith(b"\r\n"):
            part = part[:-2]
        if b"\r\n\r\n" not in part:
            continue
        raw_headers, content = part.split(b"\r\n\r\n", 1)
        if content.endswith(b"\r\n"):
            content = content[:-2]
        name = filename = None
        for line in raw_headers.decode("utf-8", "ignore").split("\r\n"):
            if line.lower().startswith("content-disposition"):
                for tok in line.split(";"):
                    tok = tok.strip()
                    if tok.startswith("name="):
                        name = tok[5:].strip('"')
                    elif tok.startswith("filename="):
                        filename = tok[9:].strip('"')
        if name:
            out[name] = {"filename": filename, "content": content}
    return out


# --------------------------------------------------------------------------
# assemble a Portfolio from the uploaded parts (pure, testable)
# --------------------------------------------------------------------------
_SUFFIX = {"portfolio": ".dat", "broker": ".csv", "cas": ".txt",
           "funds": ".csv", "fund_holdings": ".csv", "cg": ".pdf"}


def _save(tmp: Path, field: str, part: dict) -> Path:
    fn = part.get("filename") or ""
    suffix = Path(fn).suffix or _SUFFIX.get(field, ".dat")
    p = tmp / f"{field}{suffix}"
    p.write_bytes(part["content"])
    return p


def assemble_portfolio(parts: dict[str, dict], tmp: Path):
    """Build (Portfolio, compositions) from uploaded parts.

    Recognised field names: portfolio, broker, cas, cas_password, funds,
    fund_holdings. Any subset is allowed.
    """
    pf = Portfolio(meta={"source": "webapp"})
    compositions = None

    def has(name):
        return name in parts and parts[name].get("content")

    if has("portfolio"):
        loaded = load(_save(tmp, "portfolio", parts["portfolio"]))
        pf.holdings.extend(loaded.holdings)
        pf.sips.extend(loaded.sips)
        pf.meta.setdefault("replacements", []).extend(
            loaded.meta.get("replacements", []))

    if has("broker"):
        from portfolio_analyzer.sync import BrokerCSVProvider
        pf.holdings.extend(BrokerCSVProvider(_save(tmp, "broker", parts["broker"])).holdings())

    if has("cas"):
        from portfolio_analyzer.sync import load_cas
        pw = parts.get("cas_password", {}).get("content", b"").decode("utf-8", "ignore").strip()
        pf.holdings.extend(load_cas(_save(tmp, "cas", parts["cas"]), pw or None))

    if has("funds"):
        pf.holdings.extend(load_csv(_save(tmp, "funds", parts["funds"]),
                                    AssetType.MUTUAL_FUND).holdings)

    if has("fund_holdings"):
        from portfolio_analyzer.lookthrough import load_compositions
        compositions = load_compositions(_save(tmp, "fund_holdings", parts["fund_holdings"]))

    return pf, compositions


def analyze_parts(parts: dict[str, dict]) -> str:
    """Assemble -> analyse -> return dashboard HTML (or raise).

    A Capital-Gains report is a separate (tax) workflow: if one is uploaded, we
    return the CG tax reconciliation page instead of the holdings dashboard.
    """
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        if parts.get("cg", {}).get("content"):
            from portfolio_analyzer.cg import load_cg, recompute_tax
            pw = parts.get("cg_password", {}).get("content", b"").decode(
                "utf-8", "ignore").strip()
            res = load_cg(_save(tmp, "cg", parts["cg"]), pw or None)
            if not res.records:
                raise ValueError(
                    "No capital-gain rows were found in that statement. It should "
                    "be a broker Capital-Gains report (currently tuned for the "
                    "SBICAP layout). If it's a scanned/image PDF, text can't be "
                    "extracted.")
            return _cg_page(res, recompute_tax(res))

        pf, comps = assemble_portfolio(parts, tmp)
        if not pf.holdings and not pf.sips:
            raise ValueError("No holdings found in the uploaded files. Put your "
                             "tracker/Investments .xlsx in the FIRST box "
                             "(“Portfolio file”) only — not the broker or "
                             "CAS boxes.")
        a = Analysis(pf, compositions=comps)
        sugs = run_rules(a)
        return build_dashboard(pf, a, sugs, title="Portfolio Analysis")


def _cg_page(res, tax) -> str:
    def _r(x):
        return f"₹{x:,.2f}"
    rows = "".join(
        f"<tr><td>{html.escape(r.term)}</td><td>{html.escape(r.scrip)}</td>"
        f"<td class='num'>{r.buy_date}</td><td class='num'>{r.sell_date}</td>"
        f"<td class='num'>{(r.gain or 0):,.2f}</td></tr>" for r in res.records)
    terms = "".join(f"<div class='trow'><span>{html.escape(t)} total gain</span>"
                    f"<b>{_r(g)}</b></div>" for t, g in res.by_term().items())
    regimes = "".join(
        f"<div class='trow'><span>[{html.escape(d['regime'])}] "
        f"STCG {_r(d['stcg_gain'])} @ {d['stcg_rate']*100:.0f}% · "
        f"LTCG {_r(d['ltcg_gain'])} @ {d['ltcg_rate']*100:.1f}% "
        f"(−{_r(d['ltcg_exemption'])} exempt)</span>"
        f"<b>{_r(d['stcg_tax'] + d['ltcg_tax'])}</b></div>" for d in tax["by_regime"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Capital Gains — tax</title><style>
body{{margin:0;font:15px/1.55 -apple-system,Segoe UI,Roboto,Arial,sans-serif;background:#f7f8fa;color:#101828}}
.wrap{{max-width:760px;margin:0 auto;padding:32px 20px 64px}}
h1{{font-size:23px;margin:0 0 4px}}.sub{{color:#667085;margin-bottom:20px}}
.card{{background:#fff;border:1px solid #eaecf0;border-radius:12px;padding:18px;margin-bottom:14px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th,td{{text-align:left;padding:7px 8px;border-bottom:1px solid #eaecf0}}.num{{text-align:right;font-variant-numeric:tabular-nums}}
.trow{{display:flex;justify-content:space-between;padding:7px 0;border-bottom:1px dashed #eaecf0;font-size:14px}}
.disc{{color:#667085;font-size:12px;margin-top:16px}}a{{color:#3b82f6}}
</style></head><body><div class="wrap">
<h1>Capital Gains — tax reconciliation</h1>
<div class="sub">Parsed {len(res.records)} realized-gain row(s) from your statement.
This is a tax view, separate from the portfolio dashboard.</div>
<div class="card"><table>
<thead><tr><th>Term</th><th>Scrip</th><th class='num'>Buy</th><th class='num'>Sell</th><th class='num'>Gain (₹)</th></tr></thead>
<tbody>{rows}</tbody></table></div>
<div class="card">{terms}{regimes}
<div class='trow'><span><b>Total estimated tax</b> (incl. 4% cess)</span><b>{_r(tax['total_tax'])}</b></div></div>
<p class="disc"><strong>Not tax advice.</strong> Rates applied per each trade's sell-date
regime. Verify against your statement and a tax professional. <a href="/">← back</a></p>
</div></body></html>"""


# --------------------------------------------------------------------------
# pages
# --------------------------------------------------------------------------
def index_page() -> str:
    def field(name, label, hint, accept):
        return f"""
        <div class="f">
          <label for="{name}">{label}</label>
          <input type="file" id="{name}" name="{name}" accept="{accept}">
          <div class="hint">{hint}</div>
        </div>"""
    return f"""<!doctype html><html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Portfolio Analyzer</title><style>
:root{{--bg:#f7f8fa;--card:#fff;--ink:#101828;--muted:#667085;--line:#eaecf0;--acc:#3b82f6}}
*{{box-sizing:border-box}}body{{margin:0;font:15px/1.55 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink)}}
.wrap{{max-width:760px;margin:0 auto;padding:36px 20px 64px}}
h1{{font-size:26px;margin:0 0 4px}}.sub{{color:var(--muted);margin-bottom:24px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:22px;margin-bottom:16px}}
.f{{margin:14px 0}}label{{display:block;font-weight:600;font-size:14px;margin-bottom:5px}}
input[type=file]{{width:100%;padding:9px;border:1px dashed var(--line);border-radius:8px;background:#fcfcfd;font-size:13px}}
input[type=text]{{width:100%;padding:9px;border:1px solid var(--line);border-radius:8px;font-size:14px}}
.hint{{color:var(--muted);font-size:12px;margin-top:4px}}
.row{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}@media(max-width:620px){{.row{{grid-template-columns:1fr}}}}
button{{background:var(--acc);color:#fff;border:0;border-radius:9px;padding:12px 20px;font-size:15px;font-weight:600;cursor:pointer}}
button:hover{{filter:brightness(1.05)}}.ghost{{background:#fff;color:var(--ink);border:1px solid var(--line)}}
.bar{{display:flex;gap:12px;align-items:center;margin-top:8px}}
h2{{font-size:15px;margin:0 0 2px}}.disc{{color:var(--muted);font-size:12px;margin-top:18px}}
</style></head><body><div class="wrap">
<h1>Portfolio Analyzer</h1>
<div class="sub">MF + direct-equity analysis, P/L, tax, look-through — all local. Upload what you have; every field is optional.</div>
<form action="/analyze" method="post" enctype="multipart/form-data">
  <div class="card">
    <h2>Your holdings → the dashboard</h2>
    <div class="row">
      {field("portfolio","★ Portfolio file — put your Investments/tracker .xlsx HERE","Your .xlsx (or an equity CSV). This one box is all you need for the dashboard.","xlsx,csv")}
      {field("broker","Broker holdings CSV (optional)","Zerodha/Groww export: Symbol, Quantity, Average Price, Last Price. Leave empty if using the .xlsx above.","csv")}
    </div>
    <div class="row">
      {field("cas","CAS statement — mutual funds (optional)","CAMS/KFintech .pdf (PAN password below) or a .txt/.csv. NOT for a Capital-Gains report.","pdf,txt,csv")}
      <div class="f"><label for="cas_password">CAS password (PAN)</label>
        <input type="text" id="cas_password" name="cas_password" placeholder="ABCDE1234F" autocomplete="off">
        <div class="hint">Only used to open the CAS PDF locally; never stored.</div></div>
    </div>
  </div>
  <div class="card">
    <h2>Optional — fund look-through</h2>
    <div class="row">
      {field("funds","MF holdings CSV","Fund, Units, NAV, Invested — gives funds a value for look-through","csv")}
      {field("fund_holdings","Fund compositions","Fund, Stock, Weight[, Sector] (or JSON) from factsheets","csv,json")}
    </div>
  </div>
  <div class="bar">
    <button type="submit">Analyze holdings</button>
    <a href="/sample"><button type="button" class="ghost">Try with sample data</button></a>
  </div>
</form>

<form action="/analyze" method="post" enctype="multipart/form-data">
  <div class="card">
    <h2>Capital-Gains report → tax (separate)</h2>
    <div class="sub" style="margin:0 0 6px">A CG report lists trades you already
    <em>sold</em> (e.g. your SBICAP CG PDF). It produces a tax reconciliation, not
    the holdings dashboard. Upload it here on its own.</div>
    <div class="row">
      {field("cg","Capital-Gains statement (SBICAP .pdf / .txt)","Broker CG report. Realized STCG/LTCG, recomputed per year.","pdf,txt")}
      <div class="f"><label for="cg_password">PDF password (if any)</label>
        <input type="text" id="cg_password" name="cg_password" placeholder="usually your PAN" autocomplete="off">
        <div class="hint">Only used to open the PDF locally; never stored.</div></div>
    </div>
    <div class="bar"><button type="submit">Compute capital-gains tax</button></div>
  </div>
</form>
<p class="disc"><strong>Not investment advice.</strong> Files are processed in memory on this machine and not sent anywhere. Verify all figures against your broker/demat statements.</p>
</div></body></html>"""


def error_page(msg: str) -> str:
    return f"""<!doctype html><meta charset="utf-8"><title>Error</title>
<div style="max-width:640px;margin:60px auto;font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;color:#101828">
<h2 style="color:#b42318">Couldn't analyze that</h2>
<p>{html.escape(msg)}</p>
<p><a href="/">← back</a></p></div>"""


# --------------------------------------------------------------------------
# server
# --------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: str, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):  # quieter console
        pass

    def do_GET(self):
        if self.path.startswith("/sample"):
            parts = {k: {"filename": f, "content": sample_path(f).read_bytes()}
                     for k, f in (("portfolio", "example_portfolio.csv"),
                                  ("funds", "example_funds.csv"),
                                  ("fund_holdings", "example_fund_holdings.csv"))}
            try:
                return self._send(200, analyze_parts(parts))
            except Exception as e:
                return self._send(500, error_page(str(e)))
        return self._send(200, index_page())

    def do_POST(self):
        if self.path != "/analyze":
            return self._send(404, error_page("Not found"))
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            return self._send(400, error_page("Expected a multipart form upload."))
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            parts = parse_multipart(body, boundary)
            self._send(200, analyze_parts(parts))
        except Exception as e:
            traceback.print_exc()
            self._send(500, error_page(str(e)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Local web UI for the portfolio analyzer")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    args = ap.parse_args(argv)
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Portfolio Analyzer UI → http://{args.host}:{args.port}  (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
