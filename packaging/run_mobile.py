"""Phone launcher for Portfolio Analyzer (Pydroid 3 / Termux).

Open this file in Pydroid 3 and press Run, then open http://127.0.0.1:8765 in
your browser. It changes into its own folder first, so a ``gdrive.json`` +
``service_account.json`` placed next to this file are picked up for Drive sync.
"""
import os
import sys

# work from this file's folder so gdrive.json / service_account.json next to it
# are auto-discovered, and any downloads land somewhere predictable
try:
    os.chdir(os.path.dirname(os.path.abspath(__file__)))
except Exception:
    pass

try:
    from portfolio_analyzer.cli.web import main
except Exception:
    raise SystemExit(
        "Portfolio Analyzer isn't installed yet.\n"
        "Install the wheel first (see MOBILE-SETUP.txt), e.g.:\n"
        "  pip install '/storage/emulated/0/Download/"
        "portfolio_analyzer-<version>-py3-none-any.whl[mobile]'\n"
        "then run this file again.")

print("Starting Portfolio Analyzer ...")
print("Open your browser at:  http://127.0.0.1:8765")
print("(Keep this app open / in the background while you use it.)")
sys.exit(main(["--host", "127.0.0.1", "--port", "8765"]))
