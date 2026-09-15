"""Optional: push the live-priced workbook back to a file in Google Drive.

OFF by default. It activates only when the user configures BOTH a Google
service-account key file and a target Drive file id — via environment variables
or a ``gdrive.json`` sitting next to the app. No Google credentials are bundled
with the app; it uses the user's OWN service account, which can touch only the
one file they explicitly share with it.

Setup (one time, ~10 min):
  1. In Google Cloud Console, create a project, enable the Drive API, and
     create a *service account*; download its JSON key.
  2. Share your Investments sheet in Drive with the service account's email
     (the ``client_email`` in the JSON), giving it Editor access.
  3. Tell the app where things are, either with env vars:
        PA_GDRIVE_CREDENTIALS = C:\\path\\to\\service_account.json
        PA_GDRIVE_FILE_ID     = <the Drive file id from its share URL>
     or a ``gdrive.json`` next to the launcher:
        {"credentials": "service_account.json", "file_id": "..."}

After that, every analysis with live prices also updates that Drive file.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

ENV_CREDS = "PA_GDRIVE_CREDENTIALS"
ENV_FILE_ID = "PA_GDRIVE_FILE_ID"
_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def load_config(base_dir: str | Path | None = None) -> tuple[Optional[str], Optional[str]]:
    """Resolve (credentials_path, file_id) from env vars, else a gdrive.json.

    Returns (None, None) when not configured — the caller then simply skips the
    Drive push, keeping the app fully offline by default. When ``base_dir`` is
    None, a ``gdrive.json`` is looked for across the likely locations (next to a
    frozen .exe, the LocalAppData install dir, and the current directory).
    """
    creds = os.environ.get(ENV_CREDS)
    file_id = os.environ.get(ENV_FILE_ID)
    if not (creds and file_id):
        for d in ([Path(base_dir)] if base_dir else _candidate_dirs()):
            cfg = d / "gdrive.json"
            if not cfg.is_file():
                continue
            try:
                data = json.loads(cfg.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
            creds = creds or data.get("credentials")
            file_id = file_id or data.get("file_id")
            # a relative credentials path is resolved next to the config file
            if creds and not os.path.isabs(creds):
                creds = str(d / creds)
            break
    return (creds or None, file_id or None)


def _candidate_dirs() -> list[Path]:
    """Where a gdrive.json / service_account.json might sit, most specific first:
    next to a frozen .exe, the LocalAppData install dir, then the current dir."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):          # PyInstaller onefile
        dirs.append(Path(sys.executable).resolve().parent)
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        dirs.append(Path(appdata) / "PortfolioAnalyzer")
    dirs.append(Path.cwd())
    # de-dupe while preserving order
    seen, out = set(), []
    for d in dirs:
        if d not in seen:
            seen.add(d)
            out.append(d)
    return out


def is_configured(base_dir: str | Path | None = None) -> bool:
    creds, file_id = load_config(base_dir)
    return bool(creds and file_id and Path(creds).is_file())


def push_bytes(file_id: str, data: bytes, creds_path: str) -> tuple[bool, str]:
    """Replace the content of ``file_id`` in Drive with ``data`` (xlsx bytes).

    Returns (ok, message). Never raises — a failure here must not break the app.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaInMemoryUpload
    except Exception:
        return (False, "Google libraries not installed — run "
                       "pip install 'portfolio-analyzer[gdrive]'")
    try:
        creds = service_account.Credentials.from_service_account_file(
            creds_path, scopes=["https://www.googleapis.com/auth/drive"])
        svc = build("drive", "v3", credentials=creds, cache_discovery=False)
        media = MediaInMemoryUpload(data, mimetype=_XLSX_MIME, resumable=False)
        meta = svc.files().update(
            fileId=file_id, media_body=media,
            fields="id,name,modifiedTime", supportsAllDrives=True).execute()
        return (True, f"updated “{meta.get('name', file_id)}” in Google Drive")
    except Exception as e:  # auth error, not shared, wrong id, offline, …
        return (False, _friendly_error(e))


def _friendly_error(e: Exception) -> str:
    msg = str(e)
    if "404" in msg or "notFound" in msg:
        return ("Drive file not found — check the file id, and that the sheet is "
                "shared with the service account's email (Editor).")
    if "403" in msg or "insufficient" in msg.lower() or "permission" in msg.lower():
        return ("Permission denied — share the sheet with the service account's "
                "email (Editor), and ensure the Drive API is enabled.")
    return f"Google Drive update failed: {msg}"


def push_workbook(data: bytes, base_dir: str | Path | None = None) -> Optional[tuple[bool, str]]:
    """Convenience: push ``data`` using configured creds. Returns None when not
    configured (nothing to do), else (ok, message)."""
    creds, file_id = load_config(base_dir)
    if not (creds and file_id):
        return None
    if not Path(creds).is_file():
        return (False, f"credentials file not found: {creds}")
    return push_bytes(file_id, data, creds)
