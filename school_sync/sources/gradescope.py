"""Gradescope source adapter via gradescopeapi library."""

from __future__ import annotations

import html
import json
import logging
import re
import tempfile
from pathlib import Path

from gradescopeapi.classes.connection import GSConnection

from ..config import Config, CourseMapping
from ..models import Assignment

log = logging.getLogger(__name__)

_CRED_FILE = Path.home() / ".gradescope_session"


def _get_connection() -> GSConnection:
    """Authenticate using saved credentials from gradescope-cli login."""
    if not _CRED_FILE.exists():
        raise RuntimeError(
            "No Gradescope session found. Run: gradescope-cli login"
        )
    data = json.loads(_CRED_FILE.read_text())
    conn = GSConnection()
    conn.login(data["email"], data["password"])
    return conn


def _download_pdf(session, course_id: str, assignment_id: str) -> Path | None:
    """Download the assignment PDF to a temp file, if available. Returns local path."""
    url = f"https://www.gradescope.com/courses/{course_id}/assignments/{assignment_id}/submissions/new"
    try:
        resp = session.get(url, timeout=15)
        match = re.search(
            r'(https://production-gradescope-uploads\.s3[^"\'>\s]+\.pdf[^"\'>\s]*)',
            resp.text,
        )
        if not match:
            return None
        pdf_url = html.unescape(match.group(1))
        # Extract filename from URL path
        fname_match = re.search(r'/([^/?]+\.pdf)', pdf_url, re.IGNORECASE)
        filename = fname_match.group(1) if fname_match else f"assignment_{assignment_id}.pdf"
        pdf_resp = session.get(pdf_url, timeout=30)
        pdf_resp.raise_for_status()
        tmp = Path(tempfile.gettempdir()) / "school_sync_pdfs"
        tmp.mkdir(exist_ok=True)
        out = tmp / filename
        out.write_bytes(pdf_resp.content)
        return out
    except Exception:
        log.debug("Could not download PDF for assignment %s", assignment_id)
    return None


def _convert(gs_assignment, course: CourseMapping) -> Assignment | None:
    """Convert a gradescopeapi Assignment to our model"""
    status = gs_assignment.submissions_status or ""

    aid = gs_assignment.assignment_id
    return Assignment(
        external_id=f"gs:{course.gradescope_id}:{aid or 'none'}",
        title=gs_assignment.name,
        due=gs_assignment.due_date,
        course=course.course_label,
        source="Gradescope",
        link=f"https://www.gradescope.com/courses/{course.gradescope_id}/assignments/{aid}" if aid else None,
        source_status=status,
    )


def fetch_all(cfg: Config) -> list[Assignment]:
    """Fetch assignments from all configured Gradescope courses."""
    all_assignments: list[Assignment] = []
    conn = None

    for course in cfg.courses:
        if not course.gradescope_id:
            continue
        log.info("Fetching Gradescope assignments for %s (id=%s)", course.course_label, course.gradescope_id)
        try:
            if conn is None:
                conn = _get_connection()
            gs_assignments = conn.account.get_assignments(course.gradescope_id)
            parsed = [a for gs in gs_assignments if (a := _convert(gs, course))]
            log.info("  -> %d assignments", len(parsed))
            for a in parsed:
                aid = a.external_id.rsplit(":", 1)[-1]
                if aid and aid != "none":
                    pdf_path = _download_pdf(conn.session, course.gradescope_id, aid)
                    if pdf_path:
                        a.pdf_path = pdf_path
                        log.info("  -> PDF downloaded for %s", a.title)
            all_assignments.extend(parsed)
        except Exception:
            log.exception("Failed to fetch Gradescope for %s", course.course_label)

    return all_assignments
