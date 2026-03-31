# school-sync

Syncs school assignments from Gradescope and Brightspace into a Notion database, with change detection and proactive notifications via [OpenClaw](https://openclaw.ai).

Designed to run unattended on a schedule. When assignments are added, removed, or have their due dates changed, the diff is applied to Notion and a summary is pushed to Telegram.

## How it works

```
Gradescope ──(gradescopeapi)──┐
                              ├──▶ Normalize ──▶ SQLite diff ──▶ Notion upsert
Brightspace ──(ICS feed)──────┘                                  OpenClaw notify
```

1. **Poll** — Fetches assignments from Gradescope (via [gradescopeapi](https://github.com/nyuoss/gradescope-api)) and Brightspace (via direct ICS calendar feed).
2. **Normalize** — Both sources are mapped into a common `Assignment` model with a stable external ID for deduplication.
3. **Diff** — Compares current assignments against SQLite state to detect four change types: `new`, `due_changed`, `title_changed`, `removed`.
4. **Upsert** — Applies changes to a Notion database using idempotent queries on the `External ID` property. User-managed fields (Status, Estimate, Notes, Docs) are never overwritten.
5. **Notify** — Batches all changes from one sync run into a single `POST /hooks/agent` call to OpenClaw, which summarizes and delivers via Telegram.

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- A Gradescope account
- A Notion integration with access to your target database
- OpenClaw with webhooks enabled (optional, for notifications)

### Install

```bash
git clone https://github.com/meme8383/school-sync.git
cd school-sync
uv sync
```

### Configure

**1. Authenticate with Gradescope** (one-time):

```bash
uv run school-sync login
```

**2. Get your Brightspace ICS feed URL:**

In Brightspace, go to **Calendar → Subscribe** (or the calendar settings gear). Copy the ICS/webcal feed URL. It looks like:
```
https://<your-institution>.brightspace.com/d2l/le/calendar/feed/user/feed.ics?token=...
```

**3. Set up Google Drive** (required for Gradescope PDF uploads):

- Go to [Google Cloud Console](https://console.cloud.google.com)
- Enable the **Google Drive API**
- Create an **OAuth 2.0 Client ID** (Desktop app type) under **APIs & Services → Credentials**
- Download the JSON and place it at `~/.school-sync/credentials.json`
- Run the one-time auth flow:

```bash
uv run school-sync auth-drive
```

This opens a browser for Google consent and saves a token to `~/.school-sync/drive_token.json`. All subsequent runs refresh silently.

**4. Create the `.env` file:**

```bash
cp .env.example .env
```

Edit `.env` with your values. The file is loaded automatically from the project root when running `school-sync`.

Required variables:

| Variable | Description |
|---|---|
| `NOTION_API_KEY` | Your Notion integration API key |
| `NOTION_DATABASE_ID` | UUID of your Notion database |
| `BRIGHTSPACE_ICS_URL` | ICS feed URL from Brightspace Calendar → Subscribe |
| `COURSES_JSON` | JSON array of course mappings (see `.env.example`) |

### Course mappings

`COURSES_JSON` maps Brightspace organizational unit IDs to course labels. To find your OUs, look at the URL when viewing a course calendar in Brightspace (`/d2l/le/calendar/<ou>/...`):

```json
[
  {"course_label": "CS 101", "brightspace_ou": "123456", "gradescope_id": null},
  {"course_label": "MATH 201", "brightspace_ou": "789012", "gradescope_id": "345678"}
]
```

Set `gradescope_id` to the numeric course ID from Gradescope if you want that source synced, or `null` to skip it.

### Notion database schema

Your Notion database needs these properties:

| Property | Type | Purpose |
|---|---|---|
| Name | title | Assignment title |
| Due | date | Due date (ISO 8601 with timezone) |
| Course | multi_select | Course label (e.g. "CS 101") |
| External ID | rich_text | Stable dedup key (`bs:<ou>:<id>` or `gs:<course>:<id>`) |
| Source | select | "Brightspace" or "Gradescope" |
| Area | select | Always set to "School" |
| Status | status | User-managed (Not Started / In Progress / Done) |
| Link | url | Link back to source |
| Docs | files | PDF attachment (Gradescope assignments) |
| Estimate (hrs) | number | User-managed time estimate |
| Notes | rich_text | User-managed notes |

Only Name, Due, Course, External ID, Source, Area, Link, and Docs are written by the sync. Status, Estimate, and Notes are left untouched.

## Usage

```bash
# Authenticate with Gradescope (one-time)
uv run school-sync login

# Authenticate with Google Drive (one-time)
uv run school-sync auth-drive

# One-shot sync
uv run school-sync --once

# Watch mode (polls on interval, Ctrl+C to stop)
uv run school-sync --watch

# Single source only
uv run school-sync --once --source gradescope
uv run school-sync --once --source brightspace

# Preview changes without applying
uv run school-sync --once --dry-run

# Verbose logging
uv run school-sync --once -v
```

### Running on a schedule

The recommended approach is a systemd timer:

```ini
# /etc/systemd/system/school-sync.service
[Unit]
Description=School assignment sync
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory=/path/to/school-sync
ExecStart=/path/to/uv run school-sync --once
Environment=TZ=America/Indiana/Indianapolis
```

```ini
# /etc/systemd/system/school-sync.timer
[Unit]
Description=Run school-sync every 30 min during waking hours

[Timer]
OnCalendar=*-*-* 08..22:00,30:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now school-sync.timer
```

## Project structure

```
school-sync/
├── pyproject.toml              # uv/hatch project config
├── .env.example                # Config template
├── school_sync/
│   ├── main.py                 # CLI entry point (--once / --watch / login / auth-drive)
│   ├── config.py               # Env-based config with .env loader
│   ├── models.py               # Assignment and Change dataclasses
│   ├── state.py                # SQLite state layer and diff engine
│   ├── drive.py                # Google Drive PDF upload (OAuth2)
│   ├── sources/
│   │   ├── gradescope.py       # gradescopeapi library adapter
│   │   └── brightspace.py      # ICS feed adapter (stdlib urllib + parser)
│   └── targets/
│       ├── notion.py           # Notion API upsert (stdlib urllib)
│       └── openclaw.py         # OpenClaw /hooks/agent webhook
```

## Change detection

Each assignment gets a stable external ID:
- Brightspace: `bs:<organizational_unit>:<calendar_event_id>` (extracted from description URLs in the ICS feed)
- Gradescope: `gs:<course_id>:<assignment_id>`

On each sync, the current assignment set is compared against SQLite state. Four change types are detected:

| Change | Trigger | Notion action |
|---|---|---|
| `new` | External ID not in state | Create page |
| `due_changed` | Due date differs (minute precision) | Update page |
| `title_changed` | Title string differs | Update page |
| `removed` | External ID in state but not in current set | Archive page |

Past-due assignments are excluded from edits and removals. All state updates are committed atomically after Notion changes succeed.

## OpenClaw integration

When changes are detected, a single `POST /hooks/agent` request is sent to the OpenClaw gateway. The payload includes:
- A human-readable change summary
- The Notion database ID
- Page IDs of all changed items
- Run timestamp

OpenClaw runs an isolated agent turn that summarizes the changes and delivers the message to Telegram. The gateway URL and token are read from `~/.openclaw/openclaw.json` automatically.
