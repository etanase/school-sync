"""SQLite state layer for change detection."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Sequence

from .models import Assignment, Change, ChangeType


def _is_past_due(due: datetime | None) -> bool:
    """Return True if the due date is in the past."""
    if due is None:
        return False
    return due < datetime.now(timezone.utc)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS assignments (
    external_id TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    due         TEXT,
    course      TEXT NOT NULL,
    source      TEXT NOT NULL,
    link        TEXT,
    notion_page_id TEXT,
    first_seen  TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class StateDB:
    def __init__(self, db_path: str) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- reads --

    def get_all(self) -> dict[str, sqlite3.Row]:
        rows = self.conn.execute("SELECT * FROM assignments").fetchall()
        return {r["external_id"]: r for r in rows}

    def get_notion_page_id(self, external_id: str) -> str | None:
        row = self.conn.execute(
            "SELECT notion_page_id FROM assignments WHERE external_id = ?",
            (external_id,),
        ).fetchone()
        return row["notion_page_id"] if row else None

    # -- diff --

    def diff(self, current: Sequence[Assignment], *, only_sources: set[str] | None = None) -> list[Change]:
        """Compare current assignments against stored state, return changes.

        only_sources: if provided, REMOVED detection is limited to those source
        names. Use when syncing a single source so other sources' rows are not
        incorrectly flagged as removed.
        """
        stored = self.get_all()
        current_map = {a.key(): a for a in current}
        changes: list[Change] = []

        for a in current:
            old = stored.get(a.key())
            if old is None:
                changes.append(Change(ChangeType.NEW, a))
                continue

            old_due = (
                datetime.fromisoformat(old["due"]) if old["due"] else None
            )
            # Don't edit assignments that are past due
            if _is_past_due(old_due):
                continue
            if a.title != old["title"]:
                changes.append(
                    Change(ChangeType.TITLE_CHANGED, a, old_title=old["title"])
                )
            if _due_changed(a.due, old_due):
                changes.append(
                    Change(ChangeType.DUE_CHANGED, a, old_due=old_due)
                )

        for eid, old in stored.items():
            if eid not in current_map:
                # Skip removals for sources not included in this sync run
                if only_sources and old["source"] not in only_sources:
                    continue
                old_due = datetime.fromisoformat(old["due"]) if old["due"] else None
                # Don't remove assignments that are past due
                if _is_past_due(old_due):
                    continue
                removed = Assignment(
                    external_id=eid,
                    title=old["title"],
                    due=old_due,
                    course=old["course"],
                    source=old["source"],
                    link=old["link"],
                )
                changes.append(Change(ChangeType.REMOVED, removed))

        return changes

    # -- writes --

    def commit(self) -> None:
        self.conn.commit()

    def upsert(self, a: Assignment, notion_page_id: str | None = None, *, commit: bool = True) -> None:
        due_str = a.due.isoformat() if a.due else None
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            """
            INSERT INTO assignments (external_id, title, due, course, source, link, notion_page_id, first_seen, last_seen)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(external_id) DO UPDATE SET
                title = excluded.title,
                due = excluded.due,
                course = excluded.course,
                link = excluded.link,
                notion_page_id = COALESCE(excluded.notion_page_id, assignments.notion_page_id),
                last_seen = excluded.last_seen
            """,
            (a.external_id, a.title, due_str, a.course, a.source, a.link, notion_page_id, now, now),
        )
        if commit:
            self.conn.commit()

    def mark_removed(self, external_id: str, *, commit: bool = True) -> None:
        self.conn.execute(
            "DELETE FROM assignments WHERE external_id = ?", (external_id,)
        )
        if commit:
            self.conn.commit()


def _due_changed(a: datetime | None, b: datetime | None) -> bool:
    if a is None and b is None:
        return False
    if a is None or b is None:
        return True
    # Compare to the minute to avoid sub-minute jitter
    return a.replace(second=0, microsecond=0) != b.replace(second=0, microsecond=0)
