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


def entry_exists(date: str) -> bool:
    resp = requests.get(
        f"{BASE_URL}/time_entries",
        headers=HEADERS,
        params={"from": date, "to": date},
    )
    resp.raise_for_status()
    for entry in resp.json().get("time_entries", []):
        if entry["project"]["id"] == PROJECT_ID and entry["task"]["id"] == TASK_ID:
            return True
    return False


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

    resp = requests.post(
        f"{BASE_URL}/time_entries",
        headers=HEADERS,
        json=payload,
    )
    if resp.status_code == 201:
        log(f"Created {HOURS}h entry for {date}")
    elif resp.status_code == 429:
        log("Rate limited — retrying in 60s")
        time.sleep(60)
        create_entry(date, dry_run)
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
        dates = []
        d = start_date
        while d <= end_date:
            if is_workday(d):
                dates.append(d)
            d += datetime.timedelta(days=1)
    else:
        dates = [end_date]

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
