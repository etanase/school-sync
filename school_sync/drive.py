"""Google Drive integration via Google Drive API for persistent PDF storage."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

log = logging.getLogger(__name__)

_ROOT_FOLDER_NAME = "School Sync"
_SCOPES = ["https://www.googleapis.com/auth/drive.file"]
_TOKEN_FILE = Path.home() / ".school-sync" / "drive_token.json"
_CREDS_FILE = Path(os.environ.get("GOOGLE_CREDENTIALS_FILE", Path.home() / ".school-sync" / "credentials.json"))


def _get_service():
    """Return an authenticated Drive v3 service.

    On first use, opens a browser for OAuth2 consent and saves the token to
    ~/.school-sync/drive_token.json. Subsequent calls refresh silently.
    Run 'school-sync auth-drive' to trigger the initial auth flow explicitly.
    """
    creds: Credentials | None = None

    if _TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(_TOKEN_FILE), _SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            from google_auth_oauthlib.flow import InstalledAppFlow
            if not _CREDS_FILE.exists():
                raise FileNotFoundError(
                    f"Google credentials not found at {_CREDS_FILE}.\n"
                    "Download credentials.json (Desktop app) from Google Cloud Console,\n"
                    f"place it at {_CREDS_FILE}, then run: school-sync auth-drive"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(_CREDS_FILE), _SCOPES)
            creds = flow.run_local_server(port=0)

        _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        _TOKEN_FILE.write_text(creds.to_json())

    return build("drive", "v3", credentials=creds, cache_discovery=False)


def auth() -> None:
    """Trigger the OAuth2 consent flow and save credentials. Call once before first use."""
    _TOKEN_FILE.unlink(missing_ok=True)  # force fresh flow
    _get_service()
    log.info("Drive credentials saved to %s", _TOKEN_FILE)


def _find_folder(service, name: str, parent_id: str | None = None) -> str | None:
    """Find a folder by name, optionally within a parent. Returns folder ID or None."""
    q = f"mimeType='application/vnd.google-apps.folder' and name='{name}' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    resp = service.files().list(q=q, pageSize=1, fields="files(id)").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(service, name: str, parent_id: str | None = None) -> str:
    """Create a folder and return its ID."""
    meta: dict = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        meta["parents"] = [parent_id]
    data = service.files().create(body=meta, fields="id").execute()
    log.info("Created Drive folder %r (id=%s)", name, data["id"])
    return data["id"]


def _find_or_create_folder(service, name: str, parent_id: str | None = None) -> str:
    """Find an existing folder or create one. Returns folder ID."""
    fid = _find_folder(service, name, parent_id)
    return fid if fid else _create_folder(service, name, parent_id)


def _find_file(service, name: str, parent_id: str) -> str | None:
    """Find a file by name within a parent folder. Returns file ID or None."""
    q = f"name='{name}' and '{parent_id}' in parents and trashed=false"
    resp = service.files().list(q=q, pageSize=1, fields="files(id)").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def upload_pdf(local_path: Path, course: str) -> str:
    """Upload a PDF to Drive under School Sync/{course}/.

    Returns the permanent Drive view URL.
    Skips upload if a file with the same name already exists in the folder.
    """
    service = _get_service()

    root_id = _find_or_create_folder(service, _ROOT_FOLDER_NAME)
    course_id = _find_or_create_folder(service, course, root_id)

    filename = local_path.name
    existing = _find_file(service, filename, course_id)
    if existing:
        log.info("PDF already on Drive: %s (id=%s)", filename, existing)
        return f"https://drive.google.com/file/d/{existing}/view"

    media = MediaFileUpload(str(local_path), mimetype="application/pdf")
    data = service.files().create(
        body={"name": filename, "parents": [course_id]},
        media_body=media,
        fields="id",
    ).execute()
    file_id = data["id"]
    log.info("Uploaded PDF to Drive: %s (id=%s)", filename, file_id)
    return f"https://drive.google.com/file/d/{file_id}/view"
