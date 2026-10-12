"""Poll Gmail for a Workday verification email and pull out the link.

Setup (once):
  1. Google Cloud console -> create a project -> enable the Gmail API.
  2. Create an OAuth client of type "Desktop app", download it as data/credentials.json.
  3. First run opens a browser consent screen; the token is cached in data/token.json.

Scope is gmail.readonly: this code can read mail, never send or delete it.
"""

from __future__ import annotations

import base64
import html
import re
import time
from pathlib import Path

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# Workday verification links point back at the tenant, usually with an activation token.
_LINK = re.compile(r"https://[^\s\"'<>]+myworkday(?:jobs|site)?\.com/[^\s\"'<>]+", re.I)
_VERIFY_HINT = re.compile(r"verif|activat|confirm", re.I)


def extract_verification_link(body: str) -> str | None:
    links = [html.unescape(m.group(0)).rstrip(").,") for m in _LINK.finditer(body)]
    preferred = [l for l in links if _VERIFY_HINT.search(l)]
    return (preferred or links or [None])[0]


class GmailPoller:
    def __init__(self, data_dir: str | Path = "data"):
        self.data_dir = Path(data_dir)
        self._service = None

    def _svc(self):
        if self._service is None:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
            from googleapiclient.discovery import build

            token = self.data_dir / "token.json"
            creds = Credentials.from_authorized_user_file(str(token), SCOPES) if token.exists() else None
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(self.data_dir / "credentials.json"), SCOPES)
                    creds = flow.run_local_server(port=0)
                token.write_text(creds.to_json())
            self._service = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return self._service

    def wait_for_verification_link(self, to_address: str, *, since_epoch: float,
                                   timeout_s: float = 300, interval_s: float = 10) -> str | None:
        """Poll until a message to `to_address` received after `since_epoch` contains a link."""
        query = f'to:"{to_address}" after:{int(since_epoch) - 60}'
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            resp = self._svc().users().messages().list(userId="me", q=query, maxResults=5).execute()
            for ref in resp.get("messages", []):
                msg = self._svc().users().messages().get(userId="me", id=ref["id"], format="full").execute()
                if int(msg.get("internalDate", "0")) / 1000 < since_epoch - 60:
                    continue
                link = extract_verification_link(_message_text(msg["payload"]))
                if link:
                    return link
            time.sleep(interval_s)
        return None

    def find_confirmation(self, to_address: str, *, since_epoch: float) -> dict | None:
        """Look for the 'we received your application' email, for the proof log."""
        query = f'to:"{to_address}" after:{int(since_epoch) - 60} (application OR applying)'
        resp = self._svc().users().messages().list(userId="me", q=query, maxResults=5).execute()
        for ref in resp.get("messages", []):
            msg = self._svc().users().messages().get(userId="me", id=ref["id"], format="metadata").execute()
            headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
            return {"gmail_id": ref["id"], "subject": headers.get("subject", ""),
                    "from": headers.get("from", ""), "date": headers.get("date", "")}
        return None


def _message_text(payload: dict) -> str:
    parts = [payload]
    out = []
    while parts:
        p = parts.pop()
        parts.extend(p.get("parts", []))
        data = p.get("body", {}).get("data")
        if data and p.get("mimeType", "").startswith("text/"):
            out.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "replace"))
    return "\n".join(out)
