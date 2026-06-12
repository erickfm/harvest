#!/usr/bin/env python3
"""Auto-submit Harvest time entries for weekdays."""

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


def is_workday(date: datetime.date) -> bool:
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


def entry_exists(date: str) -> bool:
    """True if the date already has any time logged — including vacation or
    manual entries on other projects, which the sweep must not stack onto."""
    resp = request("GET", "/time_entries", params={"from": date, "to": date})
    resp.raise_for_status()
    return len(resp.json().get("time_entries", [])) > 0


def create_entry(date: str, dry_run: bool = False) -> None:
    payload = {
        "project_id": PROJECT_ID,
        "task_id": TASK_ID,
        "spent_date": date,
        "hours": HOURS,
        "notes": "",
    }
    if dry_run:
        log(f"DRY RUN — would POST: {json.dumps(payload)}")
        return

    resp = request("POST", "/time_entries", json=payload)
    if resp.status_code == 201:
        log(f"Created {HOURS}h entry for {date}")
    elif resp.status_code == 422:
        log(f"Error 422: {resp.text}")
        log("Project/task assignment is likely stale — check HARVEST_PROJECT_ID/HARVEST_TASK_ID "
            "against GET /v2/users/me/project_assignments")
        sys.exit(1)
    else:
        log(f"Error {resp.status_code}: {resp.text}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Auto-submit Harvest time entries")
    parser.add_argument("--date", help="Date to log (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--dry-run", action="store_true", help="Print what would happen without calling the API")
    parser.add_argument("--backfill", help="Backfill from this date (YYYY-MM-DD) through --date or today")
    args = parser.parse_args()

    end_date = datetime.date.fromisoformat(args.date) if args.date else datetime.datetime.now(TZ).date()

    if args.backfill:
        start_date = datetime.date.fromisoformat(args.backfill)
    else:
        # Self-heal: sweep the last two weeks so a crashed run can't drop a day for good.
        start_date = end_date - datetime.timedelta(days=14)
    dates = []
    d = start_date
    while d <= end_date:
        if is_workday(d):
            dates.append(d)
        d += datetime.timedelta(days=1)

    for d in dates:
        date_str = d.isoformat()
        if not is_workday(d):
            log(f"{date_str} is not a workday — skipping")
            continue
        if not args.dry_run and entry_exists(date_str):
            log(f"{date_str} already has an entry — skipping")
            continue
        create_entry(date_str, args.dry_run)


if __name__ == "__main__":
    main()
