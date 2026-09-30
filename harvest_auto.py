#!/usr/bin/env python3
"""Auto-submit Harvest time entries for weekdays, with auto-generated daily notes.

Each run sweeps the last 14 days (self-heal):
  * a workday with no time logged      → create the 8h entry, with notes
  * our entry exists but notes are ""  → fill in the notes
  * anything else                      → leave alone
Notes come from notes.py (Claude Code transcripts, cluster jobs, repo activity, calendar → Claude summary).
"""

import argparse
import datetime
import json
import os
import sys
import time
import zoneinfo

import requests
from dotenv import load_dotenv

load_dotenv()

import notes  # noqa: E402  (reads env at import; must come after load_dotenv)

TOKEN = os.environ["HARVEST_ACCESS_TOKEN"]
ACCOUNT_ID = os.environ["HARVEST_ACCOUNT_ID"]
PROJECT_ID = int(os.environ["HARVEST_PROJECT_ID"])
TASK_ID = int(os.environ["HARVEST_TASK_ID"])
HOURS = float(os.environ.get("HARVEST_HOURS", "8.0"))

BASE_URL = "https://api.harvestapp.com/v2"
HEADERS = {
    "Authorization": f"Bearer {TOKEN}",
    "Harvest-Account-Id": ACCOUNT_ID,
    "User-Agent": "HarvestAuto (harvest-auto@local)",
    "Content-Type": "application/json",
}

HOLIDAYS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "holidays.json")
TZ = zoneinfo.ZoneInfo(os.environ.get("HARVEST_TIMEZONE", "America/Los_Angeles"))


def log(msg: str) -> None:
    print(f"[{datetime.datetime.now().isoformat()}] {msg}")


def load_holidays() -> set[str]:
    if os.path.exists(HOLIDAYS_FILE):
        with open(HOLIDAYS_FILE) as f:
            return set(json.load(f))
    return set()


def nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """n-th given weekday (Mon=0) of the month; n=-1 is the last one."""
    if n > 0:
        d = datetime.date(year, month, 1)
        d += datetime.timedelta(days=(weekday - d.weekday()) % 7)
        return d + datetime.timedelta(weeks=n - 1)
    d = datetime.date(year, month + 1, 1) - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() - weekday) % 7)


def observed(d: datetime.date) -> datetime.date:
    """Saturday holidays move to Friday, Sunday holidays to Monday."""
    if d.weekday() == 5:
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:
        return d + datetime.timedelta(days=1)
    return d


def company_holidays(year: int) -> dict[datetime.date, str]:
    """AE Studio paid holidays for `year`, keyed by the weekday they are taken on."""
    mon, thu, fri = 0, 3, 4
    veterans = datetime.date(year, 11, 11)
    # Monday prior if it falls on Mon/Tue, otherwise that week's Friday.
    if veterans.weekday() <= 1:
        veterans -= datetime.timedelta(days=veterans.weekday())
    else:
        veterans += datetime.timedelta(days=fri - veterans.weekday())
    thanksgiving = nth_weekday(year, 11, thu, 4)
    christmas = observed(datetime.date(year, 12, 25))
    christmas_eve = observed(datetime.date(year, 12, 24))
    if christmas_eve >= christmas:  # both on a weekend: take the two weekdays around it
        christmas_eve = christmas - datetime.timedelta(days=1 if christmas.weekday() > mon else 3)
    return {
        observed(datetime.date(year, 1, 1)): "New Year's Day",
        nth_weekday(year, 1, mon, 3): "Martin Luther King, Jr. Day",
        nth_weekday(year, 2, mon, 3): "Presidents' Day",
        nth_weekday(year, 5, mon, -1): "Memorial Day",
        observed(datetime.date(year, 7, 4)): "Independence Day",
        nth_weekday(year, 9, mon, 1): "Labor Day",
        veterans: "Veterans Day",
        thanksgiving: "Thanksgiving",
        thanksgiving + datetime.timedelta(days=1): "Day after Thanksgiving",
        christmas_eve: "Christmas Eve",
        christmas: "Christmas Day",
    }


def holiday_name(date: datetime.date) -> str | None:
    # Next year's list too: a Saturday New Year's Day is taken on Dec 31.
    return company_holidays(date.year).get(date) or company_holidays(date.year + 1).get(date)


def is_workday(date: datetime.date) -> bool:
    """Weekday that isn't in holidays.json (manual skip list). Company holidays count as workdays
    here — they get an 8h holiday entry instead of a Gradient Routing one."""
    if date.weekday() >= 5:
        return False
    if date.isoformat() in load_holidays():
        return False
    return True


def request(method: str, path: str, **kwargs) -> requests.Response:
    """Request with timeout and retry on transient failures (429/5xx/network)."""
    for attempt in range(3):
        try:
            resp = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, timeout=30, **kwargs)
        except requests.RequestException as exc:
            log(f"Request failed ({exc}) — retrying in 60s")
            time.sleep(60)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            log(f"Got {resp.status_code} — retrying in 60s")
            time.sleep(60)
            continue
        return resp
    log("Giving up after 3 attempts")
    sys.exit(1)


def fetch_entries(start: datetime.date, end: datetime.date) -> dict[str, list[dict]]:
    """All of my time entries in [start, end], grouped by spent_date (any project)."""
    by_date: dict[str, list[dict]] = {}
    page = 1
    while True:
        resp = request("GET", "/time_entries",
                       params={"from": start.isoformat(), "to": end.isoformat(), "per_page": 100, "page": page})
        resp.raise_for_status()
        data = resp.json()
        for e in data.get("time_entries", []):
            by_date.setdefault(e["spent_date"], []).append(e)
        if not data.get("next_page"):
            return by_date
        page = data["next_page"]


def is_ours(entry: dict) -> bool:
    return entry.get("project", {}).get("id") == PROJECT_ID and entry.get("task", {}).get("id") == TASK_ID


def create_entry(date: str, note: str, dry_run: bool = False) -> None:
    payload = {
        "project_id": PROJECT_ID,
        "task_id": TASK_ID,
        "spent_date": date,
        "hours": HOURS,
        "notes": note,
    }
    if dry_run:
        log(f"DRY RUN — would POST: {json.dumps(payload)}")
        return

    resp = request("POST", "/time_entries", json=payload)
    if resp.status_code == 201:
        log(f"Created {HOURS}h entry for {date}: {note}")
    elif resp.status_code == 422:
        log(f"Error 422: {resp.text}")
        log("Project/task assignment is likely stale — check HARVEST_PROJECT_ID/HARVEST_TASK_ID "
            "against GET /v2/users/me/project_assignments")
        sys.exit(1)
    else:
        log(f"Error {resp.status_code}: {resp.text}")
        sys.exit(1)


def holiday_assignment(year: int) -> tuple[int, int] | None:
    """(project_id, task_id) for '<year> Vacation/Sick/Holiday Hours' → 'Scheduled Vacation/Day Off'."""
    resp = request("GET", "/users/me/project_assignments", params={"per_page": 100})
    resp.raise_for_status()
    for pa in resp.json().get("project_assignments", []):
        if pa["project"]["name"].startswith(f"{year} Vacation/Sick/Holiday"):
            for ta in pa["task_assignments"]:
                if ta["task"]["name"] == "Scheduled Vacation/Day Off":
                    return pa["project"]["id"], ta["task"]["id"]
    return None


def create_holiday_entry(date: datetime.date, name: str, dry_run: bool = False) -> None:
    ids = holiday_assignment(date.year)
    if ids is None:
        log(f"{date} is {name} but no '{date.year} Vacation/Sick/Holiday Hours' project is assigned — "
            "leaving it blank")
        return
    payload = {
        "project_id": ids[0],
        "task_id": ids[1],
        "spent_date": date.isoformat(),
        "hours": HOURS,
        "notes": f"{name} Holiday",
    }
    if dry_run:
        log(f"DRY RUN — would POST: {json.dumps(payload)}")
        return
    resp = request("POST", "/time_entries", json=payload)
    if resp.status_code == 201:
        log(f"Created {HOURS}h holiday entry for {date}: {name} Holiday")
    else:
        log(f"Error {resp.status_code} creating holiday entry for {date}: {resp.text}")
        sys.exit(1)


def update_notes(entry: dict, note: str, dry_run: bool = False) -> None:
    if dry_run:
        log(f"DRY RUN — would PATCH entry {entry['id']} ({entry['spent_date']}) notes: {note}")
        return
    resp = request("PATCH", f"/time_entries/{entry['id']}", json={"notes": note})
    if resp.status_code == 200:
        log(f"Updated notes for {entry['spent_date']}: {note}")
    else:
        log(f"Error {resp.status_code} updating entry {entry['id']}: {resp.text}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-submit Harvest time entries with generated notes")
    parser.add_argument("--date", help="Date to log (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without writing to Harvest")
    parser.add_argument("--backfill", help="Backfill from this date (YYYY-MM-DD) through --date or today")
    parser.add_argument("--refresh-notes", action="store_true",
                        help="Regenerate notes for our entries even if they already have notes")
    parser.add_argument("--no-notes", action="store_true", help="Skip note generation (old behaviour)")
    parser.add_argument("--notes-only", action="store_true",
                        help="Only fill notes on existing entries; never create entries. Skips today until "
                             "NOTES_TODAY_AFTER_HOUR (default 20) so the day is mostly over.")
    args = parser.parse_args()

    end_date = datetime.date.fromisoformat(args.date) if args.date else datetime.datetime.now(TZ).date()
    if args.backfill:
        start_date = datetime.date.fromisoformat(args.backfill)
    else:
        # Self-heal: sweep the last two weeks so a crashed run can't drop a day for good.
        start_date = end_date - datetime.timedelta(days=14)

    workdays = []
    d = start_date
    while d <= end_date:
        if is_workday(d):
            workdays.append(d)
        d += datetime.timedelta(days=1)

    if args.notes_only and not args.date:
        now = datetime.datetime.now(TZ)
        if now.hour < int(os.environ.get("NOTES_TODAY_AFTER_HOUR", "20")):
            log(f"notes-only before {os.environ.get('NOTES_TODAY_AFTER_HOUR', '20')}:00 — leaving today for a later run")
            workdays = [d for d in workdays if d < end_date]

    existing = fetch_entries(start_date, end_date)

    to_create: list[datetime.date] = []
    to_holiday: list[datetime.date] = []
    to_fill: dict[datetime.date, dict] = {}
    for d in workdays:
        day_entries = existing.get(d.isoformat(), [])
        if holiday_name(d):
            if day_entries:
                log(f"{d} ({holiday_name(d)}) already has an entry — leaving alone")
            elif args.notes_only:
                log(f"{d} is {holiday_name(d)} with no entry yet (Railway creates it) — skipping")
            else:
                to_holiday.append(d)
            continue
        if not day_entries:
            if args.notes_only:
                log(f"{d} has no entry yet (Railway creates it) — skipping")
            else:
                to_create.append(d)
            continue
        # Never stack onto vacation / manual entries on other projects.
        ours = [e for e in day_entries if is_ours(e)]
        if not ours:
            log(f"{d} has entries on other projects — leaving alone")
            continue
        entry = ours[0]
        if args.refresh_notes or not (entry.get("notes") or "").strip():
            to_fill[d] = entry
        else:
            log(f"{d} already logged with notes — skipping")

    for d in to_holiday:
        create_holiday_entry(d, holiday_name(d), args.dry_run)

    if not to_create and not to_fill:
        if not to_holiday:
            log("Nothing to do")
        return

    need_notes = sorted(set(to_create) | set(to_fill))
    if args.no_notes:
        day_notes = {d: "" for d in need_notes}
    else:
        day_notes = notes.build_notes(need_notes, TZ)

    for d in to_create:
        create_entry(d.isoformat(), day_notes.get(d, ""), args.dry_run)
    for d, entry in to_fill.items():
        note = day_notes.get(d, "")
        if note:
            update_notes(entry, note, args.dry_run)


if __name__ == "__main__":
    main()
