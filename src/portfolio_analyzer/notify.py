"""Alert notifiers: pluggable sinks for dispatching alerts.

All sinks are stdlib-only so the monitor runs anywhere:
  * ConsoleNotifier — prints to stdout (default)
  * FileNotifier    — appends a markdown log you can tail
  * EmailNotifier   — SMTP; configured from env or explicit args
  * WebhookNotifier — HTTP POST of the alert JSON (Slack/Discord/Zapier/etc.)

``build_from_env`` assembles the sinks from environment variables so the same
command works in cron/systemd without code changes.
"""
from __future__ import annotations

import json
import os
import smtplib
import urllib.request
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

_ICON = {"high": "🔴", "warn": "🟠", "info": "🔵"}


def _fmt_line(al) -> str:
    return f"{_ICON.get(al.severity, '•')} [{al.severity.upper()}] {al.title} — {al.message}"


class Notifier:
    def send(self, alerts: list) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class ConsoleNotifier(Notifier):
    def send(self, alerts):
        if not alerts:
            print("[monitor] no new alerts")
            return
        print(f"[monitor] {len(alerts)} alert(s):")
        for al in alerts:
            print("  " + _fmt_line(al))


class FileNotifier(Notifier):
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def send(self, alerts):
        if not alerts:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(f"\n### {ts} — {len(alerts)} alert(s)\n")
            for al in alerts:
                fh.write(f"- {_fmt_line(al)}\n")


class EmailNotifier(Notifier):
    def __init__(self, host, port, user, password, sender, to, use_tls=True):
        self.host, self.port = host, int(port)
        self.user, self.password = user, password
        self.sender, self.to = sender, to
        self.use_tls = use_tls

    def send(self, alerts):
        if not alerts:
            return
        body = "\n".join(_fmt_line(al) for al in alerts)
        n_high = sum(1 for al in alerts if al.severity == "high")
        subject = (f"[Portfolio Monitor] {len(alerts)} alert(s)"
                   + (f", {n_high} high" if n_high else ""))
        msg = MIMEText(body, _charset="utf-8")
        msg["Subject"], msg["From"], msg["To"] = subject, self.sender, self.to
        with smtplib.SMTP(self.host, self.port, timeout=30) as s:
            if self.use_tls:
                s.starttls()
            if self.user:
                s.login(self.user, self.password)
            s.sendmail(self.sender, [x.strip() for x in self.to.split(",")], msg.as_string())


class WebhookNotifier(Notifier):
    def __init__(self, url: str, text_field: str = "text"):
        self.url, self.text_field = url, text_field

    def send(self, alerts):
        if not alerts:
            return
        text = "*Portfolio Monitor*\n" + "\n".join(_fmt_line(al) for al in alerts)
        payload = {self.text_field: text,
                   "alerts": [al.to_dict() for al in alerts]}
        req = urllib.request.Request(
            self.url, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=20)  # best effort


def dispatch(alerts, notifiers) -> None:
    for n in notifiers:
        try:
            n.send(alerts)
        except Exception as e:  # a broken sink must not kill the monitor
            print(f"[monitor] notifier {type(n).__name__} failed: {e}")


def build_from_env() -> list[Notifier]:
    """Assemble notifiers from env vars; always includes the console.

    PORTFOLIO_ALERT_LOG=/path/to/alerts.md
    PORTFOLIO_WEBHOOK_URL=https://hooks.slack.com/...
    SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / ALERT_FROM / ALERT_TO
    """
    sinks: list[Notifier] = [ConsoleNotifier()]
    if os.environ.get("PORTFOLIO_ALERT_LOG"):
        sinks.append(FileNotifier(os.environ["PORTFOLIO_ALERT_LOG"]))
    if os.environ.get("PORTFOLIO_WEBHOOK_URL"):
        sinks.append(WebhookNotifier(os.environ["PORTFOLIO_WEBHOOK_URL"]))
    if os.environ.get("SMTP_HOST") and os.environ.get("ALERT_TO"):
        sinks.append(EmailNotifier(
            host=os.environ["SMTP_HOST"], port=os.environ.get("SMTP_PORT", 587),
            user=os.environ.get("SMTP_USER", ""), password=os.environ.get("SMTP_PASS", ""),
            sender=os.environ.get("ALERT_FROM", os.environ.get("SMTP_USER", "")),
            to=os.environ["ALERT_TO"]))
    return sinks
