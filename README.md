# harvest

Logs 8h/weekday to Harvest **and writes the daily note**, so Harvest never has to be opened.

Two halves:

| Where | Job | What it does |
|---|---|---|
| Railway cron (`railway.toml`, ~09:23 Pacific Mon–Fri) | `harvest_auto.py --no-notes` | creates the blank 8h entry for the day. Reliable, never sleeps. |
| This Mac, launchd (`com.erick.harvest-notes.plist`, 23:30 daily) | `harvest_auto.py --notes-only` | sweeps the last 14 days and fills every Gradient Routing entry whose notes are empty. If the Mac is asleep at 23:30 it runs at next wake and fills yesterday. |

The note-writer lives on the Mac because that's where the real record of the day is.

## What a note looks like

```
Ran confsplit aux-factor-2 runs at 200M/400M plus confidence-spread sweeps at 200M, and built
frontier/overlay and aux-factor plots to compare retain against data filtering. Chased down
partial-label hyperparameter wiring and S3 locations for the classifier data.
Meetings: Erick / Stijn, GR - Vectoring, Gradient Routing Sync Up.
```

## Where the note comes from (`notes.py`, `local_sources.py`)

| Source | Signal | Needs |
|---|---|---|
| Claude Code transcripts `~/.claude/projects/**/*.jsonl` | what you asked for that day, grouped by project | nothing |
| SLURM `sacct` over `ssh aes-cluster` | experiment job names launched that day (UTC → Pacific) | working ssh alias |
| Local repos `~/projects/*` | commits on any branch + source files edited (uncommitted counts) | nothing |
| Google Calendar secret iCal URL | timed, non-declined, non-"Free" events; all-day and focus/lunch/OOO titles dropped | `GOOGLE_CALENDAR_ICS_URL` |
| Claude (`claude-opus-5`) | turns the traces into 1–2 sentences in the required style | `ANTHROPIC_API_KEY`, else falls back to the logged-in `claude` CLI |
| `notes_overrides.json` | `{"2026-09-16": "text"}` to hand-write a day | — |
| GitHub events feed | optional extra; not needed since local repos are read directly | `GITHUB_TOKEN` |

Every source is optional. A missing or failing one is logged and skipped; the 8h entry is never
blocked. A day with no signal at all gets `HARVEST_DEFAULT_NOTE`.

## Day-to-day

Nothing. Check `harvest.log` if curious. Useful commands:

```bash
.venv/bin/python harvest_auto.py --notes-only --dry-run                 # what tonight's run would write
.venv/bin/python harvest_auto.py --notes-only --backfill 2026-08-01     # fill older blank days now
.venv/bin/python harvest_auto.py --refresh-notes --dry-run --date 2026-09-10 --backfill 2026-09-10   # redo one day
.venv/bin/python notes.py 2026-09-15 2026-09-17                         # preview notes only, no Harvest writes
launchctl kickstart -k gui/$(id -u)/com.erick.harvest-notes             # run the nightly job right now
launchctl bootout gui/$(id -u)/com.erick.harvest-notes                  # disarm it
```

Flags: `--date`, `--backfill FROM`, `--dry-run`, `--notes-only`, `--refresh-notes`, `--no-notes`.
Env knobs: see `.env.example` (`HARVEST_DEFAULT_NOTE`, `HARVEST_PROJECT_CONTEXT`, `CALENDAR_EXCLUDE_REGEX`,
`NOTES_TODAY_AFTER_HOUR`, `SLURM_SSH_HOST`, `LOCAL_REPO_GLOBS`).

## Caveats

- A day's note is written once (when its entry is found blank). `--refresh-notes` regenerates.
- `--notes-only` leaves *today* alone before 20:00 so an early-morning catch-up run doesn't
  write a half-day note.
- Company holidays (rules in `company_holidays()`) get an 8h "<Name> Holiday" entry on
  "<year> Vacation/Sick/Holiday Hours → Scheduled Vacation/Day Off" instead of Gradient Routing.
- Entries on other projects (PTO, holidays) are never touched. `holidays.json` skips dates
  for entry creation.
- If the Mac is off for more than 14 days, those days stay blank rather than getting a made-up
  note; run `--backfill` when it's back.
