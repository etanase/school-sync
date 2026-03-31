"""School assignment sync: Gradescope + Brightspace -> Notion.

Usage:
    school-sync --once                       # one-shot sync
    school-sync --watch                      # poll on interval
    school-sync --once --source gradescope   # single source
    school-sync --once --dry-run             # preview changes
    school-sync login                        # authenticate with Gradescope
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import signal
import sys
import time
from pathlib import Path

from .config import Config
from .models import Change, ChangeType
from .state import StateDB
from .sources import gradescope, brightspace
from .targets import notion, openclaw
from . import drive

log = logging.getLogger("school_sync")

_SHUTDOWN = False


def _handle_signal(sig, frame):
    global _SHUTDOWN
    log.info("Shutdown requested")
    _SHUTDOWN = True


def sync_once(cfg: Config, db: StateDB, source_filter: str | None = None, dry_run: bool = False) -> list[Change]:
    """Run one sync cycle. Returns list of changes detected."""
    # 1. Fetch from sources
    all_assignments: list[Assignment] = []

    if source_filter in (None, "gradescope"):
        try:
            gs = gradescope.fetch_all(cfg)
            all_assignments.extend(gs)
        except Exception:
            log.exception("Gradescope fetch failed")

    if source_filter in (None, "brightspace"):
        try:
            bs = brightspace.fetch_all(cfg)
            all_assignments.extend(bs)
        except Exception:
            log.exception("Brightspace fetch failed")

    log.info("Fetched %d total assignments", len(all_assignments))

    # 2. Diff against stored state (scope REMOVED detection to fetched sources only)
    _source_names = {"gradescope": "Gradescope", "brightspace": "Brightspace"}
    only_sources = {_source_names[source_filter]} if source_filter else None
    changes = db.diff(all_assignments, only_sources=only_sources)
    if not changes:
        log.info("No changes detected")
        return []

    for ch in changes:
        log.info("  %s", ch.describe())

    if dry_run:
        log.info("Dry run: %d changes would be applied", len(changes))
        return changes

    # 3. Upload PDFs to Google Drive
    for ch in changes:
        a = ch.assignment
        if a.pdf_path:
            try:
                a.pdf_url = drive.upload_pdf(a.pdf_path, a.course)
            except Exception:
                log.exception("Failed to upload PDF to Drive for %s", a.title)

    # 4. Apply to Notion
    page_ids = notion.apply_changes(cfg, changes, db.get_notion_page_id)

    # 5. Update local state (single transaction)
    for ch in changes:
        if ch.change_type == ChangeType.REMOVED:
            db.mark_removed(ch.assignment.external_id, commit=False)
        else:
            page_id = page_ids.get(ch.assignment.external_id)
            db.upsert(ch.assignment, page_id, commit=False)
    db.commit()

    # 6. Notify OpenClaw (one batched message)
    openclaw.notify(
        changes,
        enabled=cfg.openclaw_enabled,
        database_id=cfg.notion_database_id,
        page_ids=page_ids,
    )

    log.info("Sync complete: %d changes applied", len(changes))
    return changes


def cmd_login() -> None:
    """Authenticate with Gradescope and save session."""
    from gradescopeapi.classes.connection import GSConnection

    cred_file = Path.home() / ".gradescope_session"

    email = input("Gradescope email: ")
    password = getpass.getpass("Gradescope password: ")

    conn = GSConnection()
    conn.login(email, password)
    if not conn.logged_in:
        print("Login failed.")
        sys.exit(1)

    cred_file.write_text(json.dumps({"email": email, "password": password}))
    cred_file.chmod(0o600)
    print(f"Authenticated as {email}. Credentials saved to {cred_file}")


def main() -> None:
    parser = argparse.ArgumentParser(description="School assignment sync")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("login", help="Authenticate with Gradescope")
    sub.add_parser("auth-drive", help="Authenticate with Google Drive (one-time OAuth2 flow)")

    sync_parser = sub.add_parser("sync", help="Run sync (default)")
    mode = sync_parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="Run one sync cycle")
    mode.add_argument("--watch", action="store_true", help="Poll on interval")
    sync_parser.add_argument("--source", choices=["gradescope", "brightspace"],
                             help="Sync only one source")
    sync_parser.add_argument("--dry-run", action="store_true", help="Preview changes without applying")
    sync_parser.add_argument("--verbose", "-v", action="store_true", help="Debug logging")

    # Support legacy --once/--watch at top level
    parser.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--watch", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--source", choices=["gradescope", "brightspace"], help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", "-v", action="store_true", help=argparse.SUPPRESS)

    args = parser.parse_args()

    if args.command == "login":
        cmd_login()
        return

    if args.command == "auth-drive":
        logging.basicConfig(level=logging.INFO, format="%(message)s")
        drive.auth()
        print("Drive authentication complete.")
        return

    # Treat no subcommand + --once/--watch as sync
    if not args.command and not (args.once or args.watch):
        parser.print_help()
        sys.exit(1)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-5s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.from_env()
    if not cfg.notion_api_key:
        log.error("No Notion API key found. Set NOTION_API_KEY or NOTION_API_KEY_FILE.")
        sys.exit(1)

    db = StateDB(cfg.db_path)

    if args.once:
        sync_once(cfg, db, source_filter=args.source, dry_run=args.dry_run)
        db.close()
        sys.exit(0)

    if args.watch:
        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)
        log.info("Watching every %d minutes (Ctrl+C to stop)", cfg.poll_interval_minutes)

        while not _SHUTDOWN:
            try:
                sync_once(cfg, db, source_filter=args.source, dry_run=args.dry_run)
            except Exception:
                log.exception("Sync cycle failed")

            for _ in range(cfg.poll_interval_minutes * 60):
                if _SHUTDOWN:
                    break
                time.sleep(1)

        log.info("Shutting down")
        db.close()


if __name__ == "__main__":
    main()
