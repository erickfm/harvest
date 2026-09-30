"""Work signals that live on this Mac (skipped automatically when absent, e.g. on Railway).

  * Claude Code transcripts  ~/.claude/projects/**/*.jsonl  → what you asked for, per project
  * SLURM job history        ssh $SLURM_SSH_HOST sacct       → experiment job names
  * Local git repos          ~/projects/*                    → commits on any branch + files edited
"""

from __future__ import annotations

import datetime as dt
import glob
import json
import os
import re
import subprocess
import zoneinfo
from collections import Counter

HOME = os.path.expanduser("~")
CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
REPO_GLOBS = [g for g in os.environ.get("LOCAL_REPO_GLOBS", f"{HOME}/projects/*").split(":") if g]
SLURM_SSH_HOST = os.environ.get("SLURM_SSH_HOST", "aes-cluster")
SLURM_TZ = zoneinfo.ZoneInfo(os.environ.get("SLURM_TZ", "UTC"))
GIT_AUTHOR_REGEX = os.environ.get("GIT_AUTHOR_REGEX", "erick")

SOURCE_EXT = {".py", ".md", ".tex", ".ipynb", ".sh", ".yaml", ".yml", ".toml", ".ts", ".tsx", ".js", ".sql", ".R"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", "results", "outputs", "wandb", "checkpoints", "data"}

MAX_PROMPTS_PER_DAY = 40
MAX_PROMPT_CHARS = 220


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().isoformat()}] {msg}")


def available() -> bool:
    return os.path.isdir(CLAUDE_PROJECTS) or any(glob.glob(g) for g in REPO_GLOBS)


# --------------------------------------------------------------------------- #
# Claude Code transcripts
# --------------------------------------------------------------------------- #
_SYSTEMISH = re.compile(r"^\s*<(command-name|local-command|system-reminder|bash-input|task-notification)", re.I)


def claude_sessions(start: dt.date, end: dt.date, tz: zoneinfo.ZoneInfo) -> dict[dt.date, list[str]]:
    """{date: ["[project] prompt text", ...]} from the user's own prompts."""
    if not os.path.isdir(CLAUDE_PROJECTS):
        return {}
    range_start = dt.datetime.combine(start, dt.time.min, tzinfo=tz)
    range_end = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
    out: dict[dt.date, list[str]] = {}
    seen: set[tuple[dt.date, str]] = set()

    for path in glob.glob(os.path.join(CLAUDE_PROJECTS, "*", "*.jsonl")):
        try:
            if dt.datetime.fromtimestamp(os.path.getmtime(path), tz) < range_start:
                continue  # file untouched since the window began → nothing in range
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if '"type":"user"' not in line and '"type": "user"' not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if rec.get("type") != "user" or rec.get("isMeta"):
                        continue
                    ts = rec.get("timestamp")
                    if not ts:
                        continue
                    when = dt.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(tz)
                    if not (range_start <= when < range_end):
                        continue
                    text = _user_text(rec.get("message", {}).get("content"))
                    if not text or len(text) < 12 or _SYSTEMISH.match(text):
                        continue
                    project = os.path.basename(rec.get("cwd") or "") or "misc"
                    text = re.sub(r"\s+", " ", text)[:MAX_PROMPT_CHARS]
                    key = (when.date(), text[:80].lower())
                    if key in seen:
                        continue
                    seen.add(key)
                    day = out.setdefault(when.date(), [])
                    if len(day) < MAX_PROMPTS_PER_DAY:
                        day.append(f"[{project}] {text}")
        except OSError:
            continue
    return out


def _user_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(p for p in parts if p).strip()
    return ""


# --------------------------------------------------------------------------- #
# SLURM jobs via ssh
# --------------------------------------------------------------------------- #
def slurm_jobs(start: dt.date, end: dt.date, tz: zoneinfo.ZoneInfo) -> dict[dt.date, list[str]]:
    """{date: ["jobname ×3 (COMPLETED)", ...]} bucketed by local start date."""
    if not SLURM_SSH_HOST:
        return {}
    cmd = (
        f"sacct -u $USER -X -P -n --format=JobName%80,Start,State "
        f"-S {start.isoformat()}T00:00:00 -E {(end + dt.timedelta(days=2)).isoformat()}T00:00:00"
    )
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", SLURM_SSH_HOST, cmd],
            capture_output=True, text=True, timeout=90,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        log(f"sacct unavailable ({exc}) — skipping cluster jobs")
        return {}
    if res.returncode != 0:
        log(f"sacct failed: {res.stderr.strip()[:200]} — skipping cluster jobs")
        return {}

    per_day: dict[dt.date, Counter] = {}
    states: dict[tuple[dt.date, str], Counter] = {}
    for line in res.stdout.splitlines():
        parts = line.split("|")
        if len(parts) < 3 or parts[1] in ("Unknown", "None"):
            continue
        name, started, state = parts[0].strip(), parts[1].strip(), parts[2].split()[0]
        if not name or name in ("bash", "sh", "interactive", "zsh"):
            continue
        try:
            when = dt.datetime.fromisoformat(started).replace(tzinfo=SLURM_TZ).astimezone(tz)
        except ValueError:
            continue
        d = when.date()
        if not (start <= d <= end):
            continue
        per_day.setdefault(d, Counter())[name] += 1
        states.setdefault((d, name), Counter())[state] += 1

    out: dict[dt.date, list[str]] = {}
    for d, counter in per_day.items():
        items = []
        for name, n in counter.most_common(25):
            st = states[(d, name)].most_common(1)[0][0]
            items.append(f"{name}" + (f" ×{n}" if n > 1 else "") + f" ({st})")
        out[d] = items
    return out


# --------------------------------------------------------------------------- #
# Local git repos: commits on any branch + files edited (uncommitted work counts)
# --------------------------------------------------------------------------- #
def repo_activity(start: dt.date, end: dt.date, tz: zoneinfo.ZoneInfo) -> tuple[dict[dt.date, list[str]], dict[dt.date, list[str]]]:
    """Returns (commits, edited_files) keyed by date. Strings are prefixed with the repo name."""
    commits: dict[dt.date, list[str]] = {}
    edits: dict[dt.date, list[str]] = {}
    repos = sorted({p for g in REPO_GLOBS for p in glob.glob(g) if os.path.isdir(p)})
    range_start = dt.datetime.combine(start, dt.time.min, tzinfo=tz)
    range_end = dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min, tzinfo=tz)

    for repo in repos:
        name = os.path.basename(repo.rstrip("/"))
        if os.path.isdir(os.path.join(repo, ".git")):
            try:
                res = subprocess.run(
                    ["git", "-C", repo, "log", "--all", "--no-merges", "-i", f"--author={GIT_AUTHOR_REGEX}",
                     f"--since={range_start.isoformat()}", f"--until={range_end.isoformat()}",
                     "--format=%aI%x09%s"],
                    capture_output=True, text=True, timeout=30,
                )
                for line in res.stdout.splitlines():
                    ts, _, subject = line.partition("\t")
                    d = dt.datetime.fromisoformat(ts).astimezone(tz).date()
                    commits.setdefault(d, []).append(f"{name}: {subject.strip()}")
            except (subprocess.TimeoutExpired, OSError, ValueError):
                pass

        # Files edited, by mtime. Cheap walk with pruning.
        per_day: dict[dt.date, Counter] = {}
        for root, dirs, files in os.walk(repo):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
            for fn in files:
                if os.path.splitext(fn)[1] not in SOURCE_EXT:
                    continue
                fp = os.path.join(root, fn)
                try:
                    m = dt.datetime.fromtimestamp(os.path.getmtime(fp), tz)
                except OSError:
                    continue
                if range_start <= m < range_end:
                    rel = os.path.relpath(fp, repo)
                    per_day.setdefault(m.date(), Counter())[rel] += 1
        for d, counter in per_day.items():
            paths = list(counter)
            # Summarise as directories when there are many files.
            if len(paths) > 12:
                top = Counter(os.path.dirname(p) or "." for p in paths).most_common(8)
                shown = [f"{d_}/ ({n} files)" for d_, n in top]
            else:
                shown = paths
            edits.setdefault(d, []).append(f"{name}: " + ", ".join(shown))
    return commits, edits


# --------------------------------------------------------------------------- #
def gather(start: dt.date, end: dt.date, tz: zoneinfo.ZoneInfo) -> dict[str, dict[dt.date, list[str]]]:
    """All local signals, each as {date: [lines]}. Failures degrade to empty."""
    out: dict[str, dict[dt.date, list[str]]] = {}
    for label, fn in (("sessions", claude_sessions), ("jobs", slurm_jobs)):
        try:
            out[label] = fn(start, end, tz)
        except Exception as exc:
            log(f"{label} source failed ({type(exc).__name__}: {exc})")
            out[label] = {}
    try:
        out["commits"], out["edits"] = repo_activity(start, end, tz)
    except Exception as exc:
        log(f"repo source failed ({type(exc).__name__}: {exc})")
        out["commits"], out["edits"] = {}, {}
    return out
