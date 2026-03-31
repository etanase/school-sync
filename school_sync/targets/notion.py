"""Notion target: idempotent upsert of assignments into the School Tasks database."""

from __future__ import annotations

import logging
import ssl
import time
from datetime import datetime
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError
import json as _json

import certifi

_SSL_CTX = ssl.create_default_context(cafile=certifi.where())

from ..config import Config
from ..models import Assignment, Change, ChangeType

log = logging.getLogger(__name__)

_BASE = "https://api.notion.com/v1"
_VERSION = "2022-06-28"
_MAX_RETRIES = 3


def _headers(api_key: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Notion-Version": _VERSION,
    }


def _request(method: str, url: str, api_key: str, body: dict | None = None) -> dict:
    """Make an HTTP request to the Notion API with retries."""
    data = _json.dumps(body).encode() if body else None
    for attempt in range(1, _MAX_RETRIES + 1):
        req = Request(url, data=data, headers=_headers(api_key), method=method)
        try:
            with urlopen(req, context=_SSL_CTX, timeout=30) as resp:
                return _json.loads(resp.read())
        except HTTPError as e:
            resp_body = e.read().decode() if e.fp else ""
            if e.code == 429:
                wait = min(2 ** attempt, 10)
                log.warning("Notion rate-limited, retrying in %ds...", wait)
                time.sleep(wait)
                continue
            if e.code >= 500 and attempt < _MAX_RETRIES:
                time.sleep(2)
                continue
            log.error("Notion API error %d: %s", e.code, resp_body)
            raise
    raise RuntimeError("Notion API: max retries exceeded")


def _find_by_external_id(api_key: str, db_id: str, external_id: str) -> dict | None:
    """Query Notion for a page with a matching External ID."""
    body = {
        "filter": {
            "property": "External ID",
            "rich_text": {"equals": external_id},
        },
        "page_size": 1,
    }
    data = _request("POST", f"{_BASE}/databases/{db_id}/query", api_key, body)
    results = data.get("results", [])
    return results[0] if results else None


def _build_properties(a: Assignment) -> dict[str, Any]:
    """Build Notion page properties from an Assignment."""
    props: dict[str, Any] = {
        "Name": {"title": [{"text": {"content": a.title}}]},
        "External ID": {"rich_text": [{"text": {"content": a.external_id}}]},
        "Source": {"select": {"name": a.source}},
        "Course": {"multi_select": [{"name": a.course}]},
        "Area": {"select": {"name": "School"}},
    }
    if a.due:
        props["Due"] = {"date": {"start": a.due.isoformat()}}
    else:
        props["Due"] = {"date": None}
    if a.link:
        props["Link"] = {"url": a.link}
    if a.pdf_url:
        props["Docs"] = {"files": [{"type": "external", "name": "Assignment PDF", "external": {"url": a.pdf_url}}]}
    return props


def _create_page(api_key: str, db_id: str, a: Assignment) -> str:
    """Create a new page in the Notion database. Returns page ID."""
    props = _build_properties(a)
    props["Status"] = {"status": {"name": "Not Started"}}
    body = {
        "parent": {"database_id": db_id},
        "properties": props,
    }
    result = _request("POST", f"{_BASE}/pages", api_key, body)
    page_id = result["id"]
    log.info("Created Notion page %s for %s", page_id, a.external_id)
    return page_id


def _update_page(api_key: str, page_id: str, a: Assignment) -> None:
    """Update an existing Notion page."""
    body = {"properties": _build_properties(a)}
    _request("PATCH", f"{_BASE}/pages/{page_id}", api_key, body)
    log.info("Updated Notion page %s for %s", page_id, a.external_id)


def _archive_page(api_key: str, page_id: str) -> None:
    """Archive (soft-delete) a Notion page."""
    _request("PATCH", f"{_BASE}/pages/{page_id}", api_key, {"archived": True})
    log.info("Archived Notion page %s", page_id)


def upsert(cfg: Config, a: Assignment, existing_page_id: str | None = None) -> str:
    """Create or update a Notion page for the assignment. Returns page ID."""
    api_key = cfg.notion_api_key
    db_id = cfg.notion_database_id

    if existing_page_id:
        _update_page(api_key, existing_page_id, a)
        return existing_page_id

    page = _find_by_external_id(api_key, db_id, a.external_id)
    if page:
        _update_page(api_key, page["id"], a)
        return page["id"]

    return _create_page(api_key, db_id, a)


def apply_changes(cfg: Config, changes: list[Change], get_page_id) -> dict[str, str]:
    """Apply a list of changes to Notion.

    get_page_id: callable(external_id) -> str | None, to look up cached page IDs.
    Returns mapping of external_id -> notion page_id for upserted items.
    """
    page_ids: dict[str, str] = {}
    for ch in changes:
        try:
            if ch.change_type == ChangeType.REMOVED:
                page_id = get_page_id(ch.assignment.external_id)
                if page_id:
                    _archive_page(cfg.notion_api_key, page_id)
            else:
                cached_id = get_page_id(ch.assignment.external_id)
                page_id = upsert(cfg, ch.assignment, existing_page_id=cached_id)
                page_ids[ch.assignment.external_id] = page_id
        except Exception:
            log.exception("Failed to apply change for %s", ch.assignment.external_id)
    return page_ids


# -- TODO hooks for future enhancements --
# TODO: download_assignment_pdf(assignment) -> Path
# TODO: upload_to_google_drive(path, folder_id) -> drive_url
# TODO: estimate_time(assignment) -> float (hours)
# TODO: create_google_doc(assignment, template_id) -> doc_url
