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
           "funds": ".csv", "fund_holdings": ".csv"}


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
    """Assemble -> analyse -> return dashboard HTML (or raise)."""
    with tempfile.TemporaryDirectory() as td:
        pf, comps = assemble_portfolio(parts, Path(td))
        if not pf.holdings and not pf.sips:
            raise ValueError("No holdings found in the uploaded files. Check the "
                             "file formats (see the hints on the upload page).")
        a = Analysis(pf, compositions=comps)
        sugs = run_rules(a)
        return build_dashboard(pf, a, sugs, title="Portfolio Analysis")


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
    <h2>Your holdings</h2>
    <div class="row">
      {field("portfolio","Portfolio file","Tracker .xlsx (SIPs / Direct Equity / Suggested Replacements) or an equity CSV","xlsx,csv")}
      {field("broker","Broker holdings CSV","Zerodha/Groww export: Symbol, Quantity, Average Price, Last Price","csv")}
    </div>
    <div class="row">
      {field("cas","CAS statement (MF)","CAMS/KFintech .pdf (PAN password below) or a .txt/.csv export","pdf,txt,csv")}
      <div class="f"><label for="cas_password">CAS password (PAN)</label>
        <input type="text" id="cas_password" name="cas_password" placeholder="ABCDE1234F" autocomplete="off">
        <div class="hint">Only used to open the PDF locally; never stored.</div></div>
    </div>
  </div>
  <div class="card">
    <h2>Optional — look-through</h2>
    <div class="row">
      {field("funds","MF holdings CSV","Fund, Units, NAV, Invested — gives funds a value for look-through","csv")}
      {field("fund_holdings","Fund compositions","Fund, Stock, Weight[, Sector] (or JSON) from factsheets","csv,json")}
    </div>
  </div>
  <div class="bar">
    <button type="submit">Analyze</button>
    <a href="/sample"><button type="button" class="ghost">Try with sample data</button></a>
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
