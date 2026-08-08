"""Desktop launcher for the frozen (PyInstaller) Windows build.

Starts the local web UI and opens the default browser at it. Kept tiny and
dependency-free so PyInstaller can freeze it into a single .exe that needs no
Python on the target machine. Closing the console window stops the server.
"""
from __future__ import annotations

import threading
import webbrowser
from http.server import ThreadingHTTPServer

from portfolio_analyzer.cli import web

HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}"


def main() -> None:
    srv = ThreadingHTTPServer((HOST, PORT), web.Handler)
    threading.Timer(1.5, lambda: webbrowser.open(URL)).start()
    print("=" * 56)
    print("  Portfolio Analyzer is running.")
    print(f"  Open your browser at:  {URL}")
    print("  Keep this window open. Close it to stop the app.")
    print("=" * 56)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
