"""Canonical assignment model and change detection."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/Indiana/Indianapolis")


class ChangeType(Enum):
    NEW = "new"
    DUE_CHANGED = "due_changed"
    TITLE_CHANGED = "title_changed"
    REMOVED = "removed"


@dataclass
class Assignment:
    external_id: str        # stable key, e.g. "bs:123456:12345" or "gs:654321:67890"
    title: str
    due: datetime | None
    course: str             # e.g. "CS 101"
    source: str             # "Brightspace" or "Gradescope"
    link: str | None = None
    source_status: str | None = None  # raw status from source
    pdf_path: Path | None = None      # temp local path of downloaded PDF
    pdf_url: str | None = None        # permanent Drive URL after upload

    def key(self) -> str:
        return self.external_id


@dataclass
class Change:
    change_type: ChangeType
    assignment: Assignment
    old_title: str | None = None
    old_due: datetime | None = None

    @staticmethod
    def _fmt_due(dt: datetime | None) -> str:
        if dt is None:
            return "no due date"
        return dt.astimezone(_ET).strftime("%b %d %I:%M %p ET")

    def describe(self) -> str:
        a = self.assignment
        if self.change_type == ChangeType.NEW:
            return f"[NEW] {a.course}: {a.title} (due {self._fmt_due(a.due)})"
        if self.change_type == ChangeType.DUE_CHANGED:
            return f"[DUE] {a.course}: {a.title} ({self._fmt_due(self.old_due)} -> {self._fmt_due(a.due)})"
        if self.change_type == ChangeType.TITLE_CHANGED:
            return f"[TITLE] {a.course}: {self.old_title!r} -> {a.title!r}"
        if self.change_type == ChangeType.REMOVED:
            return f"[GONE] {a.course}: {a.title}"
        return f"[???] {a.external_id}"
