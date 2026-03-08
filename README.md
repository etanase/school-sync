# school-sync

Syncs school assignments from Gradescope and Brightspace into a Notion database, with change detection and proactive notifications via [OpenClaw](https://openclaw.ai).

Designed to run unattended on a schedule. When assignments are added, removed, or have their due dates changed, the diff is applied to Notion and a summary is pushed to Telegram.

## How it works

```
Gradescope ──(gradescopeapi)──┐
                              ├──▶ Normalize ──▶ SQLite diff ──▶ Notion upsert
Brightspace ──(gws calendar)──┘                                  OpenClaw notify
```

1. **Poll** — Fetches assignments from Gradescope (via the [gradescopeapi](https://github.com/nyuoss/gradescope-api) library) and Brightspace (via Google Calendar events imported through `gws`).
2. **Normalize** — Both sources are mapped into a common `Assignment` model with a stable external ID for deduplication.
3. **Diff** — Compares current assignments against SQLite state to detect four change types: `new`, `due_changed`, `title_changed`, `removed`.
4. **Upsert** — Applies changes to a Notion database using idempotent queries on the `External ID` property. User-managed fields (Status, Estimate, Notes, Docs) are never overwritten.
5. **Notify** — Batches all changes from one sync run into a single `POST /hooks/agent` call to OpenClaw, which summarizes and delivers via Telegram.

## Setup

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) (package manager)
- [gws](https://github.com/nicholasgasior/gws) authenticated (`gws auth login`)
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

Authenticate with Gradescope:

```bash
uv run school-sync login
```

Copy the example environment file into the package directory and fill in your values:

```bash
cp .env.example school_sync/.env
```

Required variables:

| Variable | Description |
|---|---|
| `NOTION_API_KEY_FILE` | Path to file containing your Notion API key |
| `NOTION_DATABASE_ID` | UUID of your Notion database |
| `BRIGHTSPACE_CALENDAR_ID` | Google Calendar ID for your Brightspace calendar import |
| `COURSES_JSON` | JSON array of course mappings (see `.env.example`) |
| `OPENCLAW_TELEGRAM_TO` | Your Telegram user ID (for notifications) |

### Notion database schema

Your Notion database needs these properties:

| Property | Type | Purpose |
|---|---|---|
| Name | title | Assignment title |
| Due | date | Due date (ISO 8601 with timezone) |
| Course | multi_select | Course label (e.g. "ECE 50863") |
| External ID | rich_text | Stable dedup key (`bs:<ou>:<id>` or `gs:<course>:<id>`) |
| Source | select | "Brightspace" or "Gradescope" |
| Status | select | User-managed (Backlog / Todo / Doing / Done) |
| Link | url | Link back to source |
| Estimate (hrs) | number | User-managed time estimate |
| Docs | files | User-managed attachments |
| Notes | rich_text | User-managed notes |

Only Name, Due, Course, External ID, Source, and Link are written by the sync. The rest are left untouched.

## Usage

```bash
# Authenticate with Gradescope (one-time)
uv run school-sync login

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
├── school_sync/
│   ├── main.py                 # CLI entry point (--once / --watch)
│   ├── config.py               # Env-based config with .env loader
│   ├── models.py               # Assignment and Change dataclasses
│   ├── state.py                # SQLite state layer and diff engine
│   ├── sources/
│   │   ├── gradescope.py       # gradescopeapi library adapter
│   │   └── brightspace.py      # gws calendar adapter
│   └── targets/
│       ├── notion.py           # Notion API upsert (stdlib urllib)
│       └── openclaw.py         # OpenClaw /hooks/agent webhook
└── .env.example
```

## Change detection

Each assignment gets a stable external ID:
- Brightspace: `bs:<organizational_unit>:<calendar_event_id>` (extracted from event description URLs)
- Gradescope: `gs:<course_id>:<assignment_id>`

On each sync, the current assignment set is compared against SQLite state. Four change types are detected:

| Change | Trigger | Notion action |
|---|---|---|
| `new` | External ID not in state | Create page |
| `due_changed` | Due date differs (minute precision) | Update page |
| `title_changed` | Title string differs | Update page |
| `removed` | External ID in state but not in current set | Archive page |

All state updates are committed atomically after Notion changes succeed.

## OpenClaw integration

When changes are detected, a single `POST /hooks/agent` request is sent to the OpenClaw gateway. The payload includes:
- A human-readable change summary
- The Notion database ID
- Page IDs of all changed items
- Run timestamp

OpenClaw runs an isolated agent turn that summarizes the changes and delivers the message to Telegram. The gateway URL and token are read from `~/.openclaw/openclaw.json` automatically.

## Future work

Hooks are left in `targets/notion.py` for:
- Downloading assignment PDFs from source
- Uploading to Google Drive
- Estimating time per assignment
- Auto-creating Google Docs from templates
