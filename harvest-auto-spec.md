# Harvest Auto-Timesheet Script — Technical Spec

## 1. Problem

You log the same 8 hours to the same project/task every workday. You forget, your accountant can't invoice, and you get in trouble. This script eliminates that by auto-submitting your time entry every weekday via the Harvest V2 API.

## 2. How the Harvest API Works

### Authentication

Every request requires **three headers**:

| Header | Value |
|---|---|
| `Authorization` | `Bearer <PERSONAL_ACCESS_TOKEN>` |
| `Harvest-Account-Id` | Your numeric account ID |
| `User-Agent` | `YourAppName (your@email.com)` — Harvest requires this |

**To get your token and account ID:**
1. Go to https://id.getharvest.com/developers
2. Click **Create New Personal Access Token**
3. Copy the token and your Account ID (shown on the same page)

### Creating a Time Entry (Duration-Based)

```
POST https://api.harvestapp.com/v2/time_entries
Content-Type: application/json

{
  "project_id": 14307913,
  "task_id": 8083365,
  "spent_date": "2026-04-01",
  "hours": 8.0,
  "notes": "Auto-logged"
}
```

- `project_id` and `task_id` are required (numeric IDs)
- `spent_date` is `YYYY-MM-DD` format
- `hours` is a decimal (8.0 for a full day)
- `notes` is optional but useful for audit trail
- Returns `201 Created` on success

> **Note:** If your Harvest account tracks time via start/end times instead of durations, you'd send `started_time` and `ended_time` instead of `hours`. Most accounts use duration mode — you can check in Harvest under Settings, or via `GET /v2/company` (look for `wants_timestamp_timers`).

### Finding Your Project & Task IDs

You need the right IDs before configuring the script. The easiest way:

```
GET https://api.harvestapp.com/v2/users/me/project_assignments
```

This returns all projects assigned to you, each with nested `task_assignments` containing the task IDs. You only need to do this once.

### Checking for Existing Entries (Idempotency)

```
GET https://api.harvestapp.com/v2/time_entries?from=2026-04-01&to=2026-04-01
```

This returns all your time entries for a given date. The script should check this **before** posting to avoid duplicate entries if it runs twice (e.g., a cron retry, manual re-run, etc.).

### Rate Limits

Harvest throttles API requests but doesn't publish exact limits. For a script making 1–2 calls per day, you'll never hit them.

---

## 3. Script Design

### Language
**Python 3** (or Bash/Node if you prefer — Python is the most portable and readable for this).

### Config
Store credentials in environment variables, not in the script:

```
HARVEST_ACCESS_TOKEN=your-token-here
HARVEST_ACCOUNT_ID=123456
HARVEST_PROJECT_ID=14307913
HARVEST_TASK_ID=8083365
HARVEST_HOURS=8.0
```

### Logic (pseudocode)

```
1. Check: is today a weekday? (Mon–Fri)
   - If no → exit silently

2. Check: is today a company holiday? (optional)
   - If yes → exit silently

3. GET /v2/time_entries?from={today}&to={today}
   - Filter results for matching project_id and task_id
   - If a matching entry already exists → exit (log "already submitted")

4. POST /v2/time_entries
   - project_id, task_id, spent_date=today, hours=8.0
   - If 201 → log success
   - If 422 → log error (usually means invalid project/task combo)
   - If 401/403 → log auth error (token expired or wrong account ID)
   - If 429 → log rate limit, retry after delay

5. (Optional) Send a notification on success/failure
   - Slack webhook, email, or macOS notification
```

### File Structure

```
harvest-auto-time/
├── harvest_auto.py       # Main script
├── .env                  # Env vars (gitignored)
├── holidays.json         # Optional: list of dates to skip
├── requirements.txt      # requests, python-dotenv
└── README.md
```

---

## 4. Scheduling

### macOS / Linux — Cron

```bash
# Run at 5:00 PM every weekday (catches end-of-day)
0 17 * * 1-5 cd /path/to/harvest-auto-time && /usr/bin/python3 harvest_auto.py >> harvest.log 2>&1
```

**Why 5 PM?** If you run it at midnight or early morning, you're logging time for a day that hasn't happened yet. End-of-day is more natural. Adjust to your timezone.

### macOS — launchd (Alternative)

If your Mac sleeps and misses cron jobs, `launchd` is more reliable — it will run missed jobs when the machine wakes up. Create a `.plist` in `~/Library/LaunchAgents/`.

### Linux Server / VPS

If you have a cloud server (even a tiny free-tier instance), cron there is the most reliable option since it never sleeps.

### Windows — Task Scheduler

Create a Basic Task → Trigger: Daily at 5 PM, weekdays only → Action: Start a Program → `python harvest_auto.py`.

---

## 5. Edge Cases & Safety

| Scenario | Handling |
|---|---|
| Script runs twice in one day | Idempotency check (step 3) prevents duplicates |
| Weekend / holiday | Weekday check + optional holiday list |
| Token expires | Personal Access Tokens don't expire unless revoked — you're fine |
| Project/task gets archived | API returns 422; log the error, you'll need to update your config |
| You take PTO | Either disable the cron job, or add a "skip dates" file the script checks |
| Laptop is asleep at 5 PM | Use `launchd` on macOS (runs on wake) or host on a server |
| Network is down | Log the failure; optionally retry or alert you |

---

## 6. Optional Enhancements

- **Slack/email notification** on success or failure — a simple webhook POST after step 4
- **PTO calendar integration** — check a Google Calendar for "OOO" events before logging
- **Backfill mode** — accept a `--date YYYY-MM-DD` argument to retroactively log missed days
- **Dry-run flag** — `--dry-run` prints what it would do without hitting the API
- **Multi-project support** — config file with multiple project/task/hours combos that sum to 8

---

## 7. Security Notes

- **Never commit your `.env` file** — add it to `.gitignore`
- Personal Access Tokens have the same permissions as your Harvest user account. Treat them like a password.
- If you're on a shared machine, make sure the `.env` file permissions are locked down (`chmod 600 .env`).

---

## 8. Quick Start Checklist

1. [ ] Generate a Personal Access Token at https://id.getharvest.com/developers
2. [ ] Note your Account ID (same page)
3. [ ] Run the project assignments endpoint to find your `project_id` and `task_id`
4. [ ] Create `.env` with your credentials
5. [ ] Write/generate the script
6. [ ] Test with `--dry-run` flag
7. [ ] Test for real with today's date
8. [ ] Set up cron / launchd / Task Scheduler
9. [ ] Verify it ran the next workday
10. [ ] Never think about timesheets again
