# Telegram Personal Account Self Client

A conservative Telethon application for sending traceable messages from one personal Telegram
account to explicitly approved groups. It uses a durable SQLite queue, sequential delivery,
idempotency, Telegram-aware retry handling, and optional control from the account's own Saved
Messages.

This is not a bulk messaging or restriction-avoidance tool. It never rotates accounts, changes
proxies to evade limits, or retries before Telegram's requested wait has elapsed.

## Requirements

- Python 3.12 or newer
- A Telegram `api_id` and `api_hash` from <https://my.telegram.org>
- A personal Telegram account that already belongs to the target groups

## Windows Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Fill in these values in `.env`:

```env
TG_API_ID=123456
TG_API_HASH=your_api_hash
TG_PHONE=+989121234567
```

The first `start` or `groups refresh` prompts for Telegram's login code and, when enabled on the
account, the 2FA password. Codes and passwords are never stored. Later runs reuse the Telethon
session at `TG_SESSION_PATH`.

## Ubuntu Server Setup

Ubuntu 24.04 LTS is recommended because the application requires Python 3.12 or newer. From the
project root, install the production service with:

```bash
sudo bash scripts/ubuntu/install.sh
sudoedit /etc/sajadbot/sajadbot.env
```

Set `TG_API_ID`, `TG_API_HASH`, and `TG_PHONE`, then perform the first Telegram login in an
interactive terminal and allow at least one destination:

```bash
sudo -u sajadbot /usr/local/bin/sajadbot groups refresh
sudo -u sajadbot /usr/local/bin/sajadbot groups list
sudo -u sajadbot /usr/local/bin/sajadbot groups allow -1001234567890
```

Start the service after authentication succeeds:

```bash
sudo systemctl enable --now sajadbot
sudo systemctl status sajadbot
sudo journalctl -u sajadbot -f
```

The installer keeps configuration in `/etc/sajadbot/sajadbot.env`, persistent state under
`/var/lib/sajadbot`, logs under `/var/log/sajadbot`, and application code under `/opt/sajadbot`.
It does not open or require an inbound network port. Re-run the installer from a newer project copy
to update the application, then restart it with `sudo systemctl restart sajadbot`.

On Ubuntu, use `sudo -u sajadbot /usr/local/bin/sajadbot <command>` in place of
`python run.py <command>` for administrative CLI commands.

## Running

Start the long-lived connection and queue worker:

```powershell
python run.py start
```

Only one process can use the configured session. Stop the existing process before starting a
second `start` or `groups refresh` command.

## Groups

Refresh groups the account can already access, inspect the cache, and explicitly allow one:

```powershell
python run.py groups refresh
python run.py groups list
python run.py groups allow -1001234567890
python run.py groups alias -1001234567890 work
python run.py groups allowed
```

Newly discovered groups are always disabled. Denying a group immediately cancels its queued,
unsent work:

```powershell
python run.py groups deny work
```

## Sending And Scheduling

Commands persist work in SQLite. A running `start` process picks it up; otherwise the work remains
queued until the next start.

```powershell
python run.py send --group work --text "Deployment completed."
python run.py send --group work --file ".\report.pdf" --text "Weekly report"
python run.py send --group work --text "Reminder" --at "2026-09-16 14:30"
```

Naive schedule values use `LOCAL_TIMEZONE`; offset-aware ISO values are also accepted. Times are
stored as UTC. Repeating an equivalent command is suppressed. Use `--force` only when an intentional
duplicate is required.

Queued media is copied beneath `data/uploads/<job-uuid>/`, so a scheduled send does not depend on
the original file remaining in place.

Set `DRY_RUN=true` to validate and record commands without calling Telegram send APIs.

## Queue And Health

```powershell
python run.py status
python run.py queue list
python run.py queue failed
python run.py queue cancel JOB_UUID
python run.py queue retry JOB_UUID
```

If the application stops while a job is `PROCESSING`, the next start moves that job to
`REVIEW_REQUIRED`. Telegram may already have accepted it, so an operator must inspect it and use
`queue retry` explicitly. This favors avoiding duplicates over automatic replay.

## Saved Messages Control

With `CONTROL_SAVED_MESSAGES=true`, send these from the authenticated account to its own Saved
Messages:

```text
/help
/status
/groups
/groups all
/groups refresh
/groups add <public-link|@username|chat-id> [alias]
/groups remove <chat-id|alias>
/send work
message text
/schedule work 2026-09-16 14:30
message text
/queue
/cancel <job-uuid>
/logs [5-300 seconds]
/logs stop
```

Multiple groups can be allowed in one command by putting one reference and optional alias on each
line after `/groups add`. Public `t.me` links and `@username` references are accepted. The client
never auto-joins a private invite link: join with the personal account first, run `/groups refresh`,
then allow the cached chat ID. `/logs` works with a personal account by repeatedly editing the
command message; it shows only the bounded, redacted in-memory log view. Dot-prefixed legacy
commands such as `.status` remain supported.

Commands from groups, direct messages, other senders, or forwarded messages are ignored. The
controller replies when a job is queued and again after delivery or failure.

## Reliability Behavior

- Sends are sequential and paced by `DEFAULT_SEND_INTERVAL_SECONDS`.
- `FloodWaitError` and group slow mode are persisted with their required next-attempt time.
- A FloodWait above `MAX_AUTOMATIC_FLOOD_WAIT_SECONDS` pauses all outgoing delivery until its time.
- Temporary network failures use capped exponential backoff with jitter.
- Authorization failures stop the worker and require operator login.
- Permission, destination, and invalid-content errors fail without blind retries.
- Logs rotate at 10 MB with five backups and omit message bodies by default.

## Security

The `.session` file is a credential. Anyone who obtains it may be able to access the Telegram
account. Keep `data/sessions` private, exclude it from backups that are not encrypted, and never
serve it through HTTP or static hosting. The repository ignores `.env`, session files, databases,
uploads, and logs.

Do not put secrets on command lines or in logs. `TG_API_HASH`, authorization codes, 2FA passwords,
and session contents are intentionally absent from application records.

## Troubleshooting

- **FloodWait / SlowMode:** inspect `status` and `queue list`. The worker waits until Telegram's
  specified time; do not restart repeatedly to bypass it.
- **Session unauthorized:** run `start` interactively and authenticate. Outgoing work stays stopped.
- **Database is locked:** another application process may be active. Stop it instead of retrying
  aggressively.
- **Another process using session:** only one `start` or `groups refresh` may own a session.
- **Group write forbidden:** refresh groups and verify membership/permissions. The job fails and the
  destination is marked non-sendable.
- **Network disconnected:** Telethon reconnects automatically; queued work retries conservatively.
- **Invalid chat ID:** run `groups refresh` and use an ID or alias shown by `groups list`.

## Tests

Tests use fakes and never contact Telegram:

```powershell
pytest
ruff check .
ruff format --check .
```
