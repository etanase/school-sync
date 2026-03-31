"""Brightspace source adapter via direct ICS feed."""

from __future__ import annotations

import logging
import re
import ssl
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import certifi

from ..config import Config, CourseMapping
from ..models import Assignment

log = logging.getLogger(__name__)


def _fetch_ics(url: str) -> str:
    """Fetch the ICS feed URL and return raw text."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(url, context=ctx, timeout=30) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _unfold_ics(text: str) -> str:
    """Remove RFC 5545 line folding (CRLF/LF + single space or tab = continuation)."""
    text = text.replace("\r\n ", "").replace("\r\n\t", "")
    text = text.replace("\n ", "").replace("\n\t", "")
    return text


def _unescape_ics(value: str) -> str:
    """Unescape ICS property value sequences."""
    return value.replace("\\n", "\n").replace("\\,", ",").replace("\\\\", "\\")


def _parse_ics_events(text: str) -> list[dict]:
    """Parse VEVENT blocks from an unfolded ICS text into a list of dicts."""
    events: list[dict] = []
    current: dict | None = None

    for line in text.splitlines():
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None:
            continue

        # Split property name+params from value on the first ":"
        prop_full, _, value = line.partition(":")
        prop_name = prop_full.split(";")[0].upper()

        # Extract TZID param if present (e.g. DTSTART;TZID=America/Indiana/Indianapolis)
        tzid: str | None = None
        for part in prop_full.split(";")[1:]:
            if part.upper().startswith("TZID="):
                tzid = part[5:]
            elif part.upper().startswith("VALUE=DATE"):
                # Mark as date-only by storing a sentinel
                value = f"DATE:{value}"

        value = _unescape_ics(value)

        if prop_name == "SUMMARY":
            current["summary"] = value
        elif prop_name == "DESCRIPTION":
            current["description"] = value
        elif prop_name == "URL":
            current["url"] = value
        elif prop_name == "UID":
            current["uid"] = value
        elif prop_name == "DTSTART":
            current["dtstart_value"] = value
            current["dtstart_tzid"] = tzid
        elif prop_name == "DTEND":
            current["dtend_value"] = value
            current["dtend_tzid"] = tzid

    return events


def _parse_ics_dt(value: str, tzid: str | None) -> datetime | None:
    """Parse an ICS DTSTART/DTEND value into a timezone-aware datetime."""
    if not value:
        return None
    # All-day event: VALUE=DATE sentinel or bare 8-digit date
    if value.startswith("DATE:"):
        value = value[5:]
    if re.fullmatch(r"\d{8}", value):
        dt = datetime.strptime(value, "%Y%m%d").replace(hour=23, minute=59)
        return dt.replace(tzinfo=ZoneInfo(tzid)) if tzid else dt
    # UTC
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    # Floating or TZID-qualified
    try:
        dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
    except ValueError:
        return None
    return dt.replace(tzinfo=ZoneInfo(tzid)) if tzid else dt


def _is_availability(summary: str) -> bool:
    """Filter out availability windows, not actual assignments."""
    s = (summary or "").strip()
    if s.endswith(" - Available"):
        return True
    if re.search(r"\bAvailability\s*Ends\b", s, flags=re.I):
        return True
    if re.search(r"\bAvailable\b", s, flags=re.I) and not s.endswith(" - Due"):
        return True
    return False


def _normalize_title(summary: str) -> str:
    s = (summary or "").strip()
    if s.endswith(" - Due"):
        s = s[:-6]
    return s.strip()


def _extract_brightspace_key(description: str) -> str | None:
    """Extract bs:<ou>:<event_id> from the event description URL."""
    if not description:
        return None
    m = re.search(r"/d2l/le/calendar/(\d+)/event/(\d+)/", description)
    if not m:
        return None
    return f"bs:{m.group(1)}:{m.group(2)}"


def _extract_ou(description: str) -> str | None:
    if not description:
        return None
    m = re.search(r"\bou=(\d+)\b", description)
    if m:
        return m.group(1)
    m = re.search(r"/d2l/le/calendar/(\d+)/", description)
    return m.group(1) if m else None


def fetch_all(cfg: Config) -> list[Assignment]:
    """Fetch Brightspace assignments via ICS feed."""
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    time_min = now.replace(hour=0, minute=0, second=0, microsecond=0)
    time_max = time_min + timedelta(days=cfg.sync_days_ahead)

    # Build OU -> course mapping
    ou_map: dict[str, CourseMapping] = {
        c.brightspace_ou: c for c in cfg.courses if c.brightspace_ou
    }

    log.info("Fetching Brightspace ICS feed")
    try:
        raw = _fetch_ics(cfg.brightspace_ics_url)
        raw_events = _parse_ics_events(_unfold_ics(raw))
    except Exception:
        log.exception("Failed to fetch Brightspace ICS")
        return []

    assignments: list[Assignment] = []
    for ev in raw_events:
        summary = ev.get("summary", "")
        if _is_availability(summary):
            continue

        due = _parse_ics_dt(ev.get("dtstart_value", ""), ev.get("dtstart_tzid"))
        if due is None:
            due = _parse_ics_dt(ev.get("dtend_value", ""), ev.get("dtend_tzid"))

        # Normalize to aware datetime using config timezone
        if due is not None and due.tzinfo is None:
            due = due.replace(tzinfo=tz)

        # Time-window filter (replaces API-side timeMin/timeMax)
        if due is None or not (time_min <= due <= time_max):
            continue

        description = ev.get("description", "")
        ou = _extract_ou(description)
        bs_key = _extract_brightspace_key(description)
        if not bs_key:
            uid = ev.get("uid", "")
            bs_key = f"bs:{ou}:{uid}" if ou else f"bs:unknown:{uid}"

        course_map = ou_map.get(ou) if ou else None
        course_label = course_map.course_label if course_map else "Unknown"

        title = _normalize_title(summary)

        # Prefer URL property, fall back to regex on description
        link: str | None = ev.get("url") or None
        if not link:
            link_m = re.search(r"(https://[a-zA-Z0-9.-]+\.brightspace\.com/[^\s<\"]+)", description)
            if link_m:
                link = link_m.group(1)

        assignments.append(Assignment(
            external_id=bs_key,
            title=title,
            due=due,
            course=course_label,
            source="Brightspace",
            link=link,
            source_status=None,
        ))

    log.info("  -> %d Brightspace assignments after filtering", len(assignments))
    return assignments
