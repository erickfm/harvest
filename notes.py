"""Build the daily Harvest note from automatic signals.

Signals (each optional — missing config just drops that source):
  * Google Calendar  — secret iCal URL (GOOGLE_CALENDAR_ICS_URL) → meeting names
  * GitHub           — user events feed (GITHUB_TOKEN) → commits / PRs / reviews
  * Overrides        — notes_overrides.json {"YYYY-MM-DD": "text"} → verbatim work description
  * Claude           — ANTHROPIC_API_KEY → one-line summary of the GitHub activity

Output shape (what lands in Harvest):
  "<work description>  Meetings: <A>, <B>."
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import zoneinfo
from dataclasses import dataclass, field

import requests
from dotenv import load_dotenv

load_dotenv()  # must run before the env-derived constants below

HERE = os.path.dirname(os.path.abspath(__file__))
OVERRIDES_FILE = os.path.join(HERE, "notes_overrides.json")

DEFAULT_NOTE = os.environ.get(
    "HARVEST_DEFAULT_NOTE",
    "Gradient routing / GRAM research: experiment monitoring, analysis and code work.",
)
PROJECT_CONTEXT = os.environ.get(
    "HARVEST_PROJECT_CONTEXT",
    "Gradient routing / GRAM research at AE Studio (AIAF-funded): training-run "
    "babysitting, data filtering and classification, evaluation, paper writing.",
)

CALENDAR_ICS_URL = os.environ.get("GOOGLE_CALENDAR_ICS_URL", "")
CALENDAR_EXCLUDE = re.compile(
    os.environ.get(
        "CALENDAR_EXCLUDE_REGEX",
        r"focus|lunch|\booo\b|out of office|do not book|\bdnd\b|busy|hold|blocked?\b|commute|gym",
    ),
    re.IGNORECASE,
)

GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_LOGIN = os.environ.get("GITHUB_LOGIN", "")
GITHUB_AUTHOR_MATCH = re.compile(
    os.environ.get("GITHUB_AUTHOR_REGEX", r"erick"), re.IGNORECASE
)

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().isoformat()}] {msg}")


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass
class DaySignals:
    date: dt.date
    meetings: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)   # "repo: subject"
    prs: list[str] = field(default_factory=list)       # "repo#12 opened: title"
    sessions: list[str] = field(default_factory=list)  # "[project] what I asked Claude Code for"
    jobs: list[str] = field(default_factory=list)      # "jobname ×3 (COMPLETED)"
    edits: list[str] = field(default_factory=list)     # "repo: files touched"
    override: str | None = None

    @property
    def has_work_signal(self) -> bool:
        return bool(self.commits or self.prs or self.sessions or self.jobs or self.edits or self.override)


# --------------------------------------------------------------------------- #
# Google Calendar (secret iCal address — no OAuth needed)
# --------------------------------------------------------------------------- #
def fetch_calendar(start: dt.date, end: dt.date, tz: zoneinfo.ZoneInfo) -> dict[dt.date, list[str]]:
    """Meeting titles per local date, inclusive range. Empty dict if not configured."""
    if not CALENDAR_ICS_URL:
        return {}
    try:
        import icalendar
        import recurring_ical_events
    except ImportError:
        log("icalendar/recurring_ical_events not installed — skipping calendar")
        return {}

    resp = requests.get(CALENDAR_ICS_URL, timeout=60)
    resp.raise_for_status()
    cal = icalendar.Calendar.from_ical(resp.content)

    range_start = dt.datetime.combine(start, dt.time.min, tzinfo=tz)
    range_end = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=tz)

    out: dict[dt.date, list[str]] = {}
    seen: set[tuple[dt.date, str]] = set()
    for ev in recurring_ical_events.of(cal).between(range_start, range_end):
        title = re.sub(r"\s+", " ", str(ev.get("SUMMARY", ""))).strip()
        if not title:
            continue
        dtstart = ev.get("DTSTART").dt
        if not isinstance(dtstart, dt.datetime):
            continue  # all-day event (birthday, OOO block, holiday) — not a meeting
        if str(ev.get("TRANSP", "OPAQUE")).upper() == "TRANSPARENT":
            continue  # marked "Free"
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        if _declined_by_me(ev):
            continue
        if CALENDAR_EXCLUDE.search(title):
            continue
        if dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=tz)
        local_date = dtstart.astimezone(tz).date()
        key = (local_date, title.lower())
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(local_date, []).append(title)
    return out


def _declined_by_me(ev) -> bool:
    """True if the calendar owner's ATTENDEE line says DECLINED."""
    my_email = os.environ.get("CALENDAR_OWNER_EMAIL", "").lower()
    attendees = ev.get("ATTENDEE", [])
    if not isinstance(attendees, list):
        attendees = [attendees]
    for att in attendees:
        addr = str(att).lower().replace("mailto:", "")
        if my_email and addr != my_email:
            continue
        params = getattr(att, "params", {}) or {}
        if str(params.get("PARTSTAT", "")).upper() == "DECLINED":
            return True
    return False


# --------------------------------------------------------------------------- #
# GitHub — the authenticated user's event stream (covers all branches & private repos)
# --------------------------------------------------------------------------- #
def fetch_github(start: dt.date, end: dt.date, tz: zoneinfo.ZoneInfo) -> dict[dt.date, DaySignals]:
    """Commits and PR activity per local date. Empty dict if not configured."""
    if not GITHUB_TOKEN:
        return {}
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "HarvestAuto",
    }
    login = GITHUB_LOGIN
    if not login:
        me = requests.get("https://api.github.com/user", headers=headers, timeout=30)
        me.raise_for_status()
        login = me.json()["login"]

    range_start = dt.datetime.combine(start, dt.time.min, tzinfo=tz)
    out: dict[dt.date, DaySignals] = {}
    seen_shas: set[str] = set()

    for page in range(1, 11):  # API caps at 300 events / 10 pages
        resp = requests.get(
            f"https://api.github.com/users/{login}/events",
            headers=headers, params={"per_page": 100, "page": page}, timeout=30,
        )
        if resp.status_code == 422:
            break  # past the pagination limit
        resp.raise_for_status()
        events = resp.json()
        if not events:
            break
        stop = False
        for ev in events:
            created = dt.datetime.fromisoformat(ev["created_at"].replace("Z", "+00:00")).astimezone(tz)
            if created < range_start:
                stop = True
                break
            day = created.date()
            if day > end:
                continue
            sig = out.setdefault(day, DaySignals(date=day))
            repo = ev["repo"]["name"].split("/")[-1]
            payload = ev.get("payload", {})
            etype = ev["type"]
            if etype == "PushEvent":
                for c in payload.get("commits", []):
                    if c["sha"] in seen_shas:
                        continue
                    author = f"{c.get('author', {}).get('name', '')} {c.get('author', {}).get('email', '')}"
                    if not GITHUB_AUTHOR_MATCH.search(author):
                        continue
                    subject = c["message"].splitlines()[0].strip()
                    if subject.lower().startswith("merge "):
                        continue
                    seen_shas.add(c["sha"])
                    sig.commits.append(f"{repo}: {subject}")
            elif etype == "PullRequestEvent":
                pr = payload.get("pull_request", {})
                action = payload.get("action")
                if action == "closed" and pr.get("merged"):
                    action = "merged"
                if action in ("opened", "merged", "reopened"):
                    sig.prs.append(f"{repo}#{pr.get('number')} {action}: {pr.get('title', '')}")
            elif etype == "PullRequestReviewEvent":
                pr = payload.get("pull_request", {})
                sig.prs.append(f"{repo}#{pr.get('number')} reviewed: {pr.get('title', '')}")
        if stop:
            break
    return out


# --------------------------------------------------------------------------- #
# Overrides
# --------------------------------------------------------------------------- #
def load_overrides() -> dict[str, str]:
    if os.path.exists(OVERRIDES_FILE):
        with open(OVERRIDES_FILE) as f:
            data = json.load(f)
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return {}


# --------------------------------------------------------------------------- #
# Summarisation
# --------------------------------------------------------------------------- #
SYSTEM_PROMPT = f"""You write one-line daily timesheet notes for an AI-safety researcher.
Project context: {PROJECT_CONTEXT}

You are given raw traces of the day: things the researcher asked their coding assistant for
(grouped by project), cluster jobs launched, files edited, and git commits. Write ONE short
plain-text description (1–2 sentences, under 240 characters) of what was worked on that day,
in the style of these real examples:
  "Abstract writing for next GRAM paper. TL Gram Experiment babysitting at 224M, data filtering and classification work."
  "Confidence-abstention pipeline for the classifier split; D2 plots and 200M baseline config."

Rules:
- Describe only work evidenced by the input. Never invent activities.
- Group the traces into 2–4 themes (experiments run, analysis, pipeline/code work, writing).
  Name the experiment families and model sizes when the job names make them clear
  (e.g. "confsplit 200M/400M sweeps", "retain-set audit").
- Phrase it as the researcher's own work ("Ran…", "Built…", "Analysed…"), never as
  "asked Claude to…". Ignore anything unrelated to the research project (personal
  errands, timesheet tooling, admin) unless nothing else happened that day.
- No commit hashes, ticket numbers, file paths, quotes, markdown, or leading labels.
- Do not mention meetings; they are appended separately.
- Output the sentence(s) only."""


def _signal_lines(sig: DaySignals) -> list[str]:
    lines = [f"Date: {sig.date.isoformat()} ({sig.date.strftime('%A')})"]
    for label, items in (("Coding-assistant requests", sig.sessions), ("Cluster jobs launched", sig.jobs),
                         ("Files edited", sig.edits), ("Commits", sig.commits), ("Pull requests", sig.prs)):
        if items:
            lines.append(f"{label}:")
            lines += [f"- {i}" for i in items]
    return lines


def summarize_with_claude(sig: DaySignals) -> str | None:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return _summarize_sdk(sig)
    return _summarize_cli(sig)


def _summarize_cli(sig: DaySignals) -> str | None:
    """Fallback when no API key: use the locally logged-in Claude Code CLI in print mode."""
    import shutil
    import subprocess
    exe = os.environ.get("CLAUDE_CLI", shutil.which("claude") or os.path.expanduser("~/.local/bin/claude"))
    if not os.path.exists(exe):
        return None
    prompt = SYSTEM_PROMPT + "\n\n" + "\n".join(_signal_lines(sig))
    try:
        res = subprocess.run(
            [exe, "-p", "--output-format", "text", "--tools", "", "--no-session-persistence"],
            input=prompt, capture_output=True, text=True, timeout=240,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"claude CLI failed ({exc}) — using plain summary")
        return None
    if res.returncode != 0:
        log(f"claude CLI exit {res.returncode}: {res.stderr.strip()[:200]} — using plain summary")
        return None
    text = re.sub(r"\s+", " ", res.stdout).strip().strip('"')
    return text or None


def _summarize_sdk(sig: DaySignals) -> str | None:
    try:
        import anthropic
    except ImportError:
        log("anthropic SDK not installed — using plain summary")
        return None

    lines = _signal_lines(sig)
    try:
        client = anthropic.Anthropic(timeout=120.0, max_retries=2)
        resp = client.beta.messages.create(
            model=ANTHROPIC_MODEL,
            max_tokens=2000,
            system=SYSTEM_PROMPT,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": "\n".join(lines)}],
        )
        if resp.stop_reason == "refusal":
            log("Claude declined to summarise — using plain summary")
            return None
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        text = re.sub(r"\s+", " ", text).strip().strip('"')
        return text or None
    except Exception as exc:  # any API failure → deterministic fallback, never block the timesheet
        log(f"Claude summary failed ({type(exc).__name__}: {exc}) — using plain summary")
        return None


def plain_summary(sig: DaySignals) -> str:
    """Deterministic fallback when no summariser is available: join the strongest raw signal."""
    items = sig.commits[:4] or sig.prs[:4] or sig.jobs[:4] or sig.edits[:3] or sig.sessions[:3]
    text = "; ".join(i.split(": ", 1)[-1].rstrip(".") for i in items)
    if not text:
        return DEFAULT_NOTE
    return text + "."


def compose(sig: DaySignals) -> str:
    if sig.override:
        work = sig.override.strip()
    elif sig.has_work_signal:
        work = summarize_with_claude(sig) or plain_summary(sig)
    else:
        work = DEFAULT_NOTE
    if not work.endswith((".", "!", "?")):
        work += "."
    if sig.meetings:
        work += " Meetings: " + ", ".join(sig.meetings) + "."
    return work


# --------------------------------------------------------------------------- #
# Public entry point
# --------------------------------------------------------------------------- #
def build_notes(dates: list[dt.date], tz: zoneinfo.ZoneInfo) -> dict[dt.date, str]:
    """Return {date: note} for every requested date (always returns something per date)."""
    if not dates:
        return {}
    start, end = min(dates), max(dates)

    try:
        calendar = fetch_calendar(start, end, tz)
    except Exception as exc:
        log(f"Calendar fetch failed ({type(exc).__name__}: {exc}) — notes will omit meetings")
        calendar = {}
    try:
        github = fetch_github(start, end, tz)
    except Exception as exc:
        log(f"GitHub fetch failed ({type(exc).__name__}: {exc}) — notes will omit code activity")
        github = {}
    overrides = load_overrides()

    local: dict[str, dict[dt.date, list[str]]] = {}
    try:
        import local_sources
        if local_sources.available():
            local = local_sources.gather(start, end, tz)
    except Exception as exc:
        log(f"Local sources failed ({type(exc).__name__}: {exc})")

    notes: dict[dt.date, str] = {}
    for d in dates:
        sig = github.get(d, DaySignals(date=d))
        sig.meetings = calendar.get(d, [])
        sig.sessions = local.get("sessions", {}).get(d, [])
        sig.jobs = local.get("jobs", {}).get(d, [])
        sig.edits = local.get("edits", {}).get(d, [])
        for c in local.get("commits", {}).get(d, []):
            if c not in sig.commits:
                sig.commits.append(c)
        sig.override = overrides.get(d.isoformat())
        notes[d] = compose(sig)
        log(f"{d}: {len(sig.sessions)} requests, {len(sig.jobs)} job groups, {len(sig.edits)} repos edited, "
            f"{len(sig.commits)} commits, {len(sig.meetings)} meetings" + (" (override)" if sig.override else ""))
    return notes


if __name__ == "__main__":  # quick manual check: python notes.py 2026-09-15 [2026-09-17]
    import sys
    tz = zoneinfo.ZoneInfo(os.environ.get("HARVEST_TIMEZONE", "America/Los_Angeles"))
    a = dt.date.fromisoformat(sys.argv[1]) if len(sys.argv) > 1 else dt.datetime.now(tz).date()
    b = dt.date.fromisoformat(sys.argv[2]) if len(sys.argv) > 2 else a
    days = [a + dt.timedelta(days=i) for i in range((b - a).days + 1) if (a + dt.timedelta(days=i)).weekday() < 5]
    for d, n in build_notes(days, tz).items():
        print(f"{d}  {n}")
