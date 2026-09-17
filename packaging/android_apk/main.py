"""Android (python-for-android 'webview' bootstrap) entry point.

Starts the same local web app on 127.0.0.1:5000 in a background thread; the
p4a webview bootstrap shows it in an Android WebView. Everything runs on the
phone — no server, same as the desktop app.
"""
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _serve():
    from portfolio_analyzer.cli.web import main as web_main
    web_main(["--host", "127.0.0.1", "--port", "5000"])


if __name__ == "__main__":
    threading.Thread(target=_serve, daemon=True).start()
    # keep the process alive; the WebView (bootstrap) loads http://127.0.0.1:5000
    while True:
        time.sleep(1)
