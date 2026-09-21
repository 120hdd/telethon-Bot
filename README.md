# Telegram Personal Account Self Client

A conservative Telethon application for sending traceable messages from one personal Telegram
account to explicitly approved groups. It uses a durable SQLite queue, sequential delivery,
idempotency, Telegram-aware retry handling, and optional control from the account's own Saved
Messages.

This is not a bulk messaging or restriction-avoidance tool. It never rotates accounts, changes
proxies to evade limits, or retries before Telegram's requested wait has elapsed.

## Requirements

- Python 3.12 or newer
- A custom Telegram `api_id`/`api_hash`, or an intentionally configured shared/public pair
- A personal Telegram account that already belongs to the target groups

## Windows Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Fill in the applicable values in `.env`:

```env
TELEGRAM_PHONE=+10000000000
TELEGRAM_API_MODE=auto
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PUBLIC_API_ID=
TELEGRAM_PUBLIC_API_HASH=
TELEGRAM_SESSION_DIR=./data/sessions
```

## Telegram authentication

The API ID and API hash identify the Telegram API application/client. They do **not** select the
Telegram account. The phone-number authorization flow selects the real Telegram **user** account
that the persistent Telethon session controls; this is not a BotFather bot-token flow.

After installing dependencies and copying `.env.example` to `.env`:

1. Set `TELEGRAM_PHONE` in international format.
2. Choose `TELEGRAM_API_MODE=custom`, `public`, or `auto`. `auto` prefers a complete custom pair
   and otherwise uses the complete public pair. IDs and hashes are never mixed across profiles.
3. Run `python -m app auth login` (or add `--qr` to scan Telegram's QR URL).
4. Enter the code Telegram delivers. Telegram may deliver it inside another logged-in app, by
   email, or by another mechanism it chooses. Enter the 2FA password if securely prompted.
5. Verify with `python -m app auth status`.
6. Validate without Telegram mutations using `python -m app start --dry-run`.
7. Start production using `python -m app start`.

Normal startup never asks for a login code. If authorization is missing, it exits and tells the
operator to run the explicit login command. A wrong code can be entered again without requesting a
new code. An expired code requires a new explicit login attempt.

Sessions are separated by API profile as `account_custom.session` and `account_public.session`
under `TELEGRAM_SESSION_DIR`. A `*.session` file is a highly sensitive credential: never share it,
commit it, print it, or place it in an unencrypted backup. To deliberately reset only the selected
profile, stop the service and run `python -m app auth reset`; interactive confirmation is required,
or `--yes` must be supplied.

A shared/published API ID is only a configurable fallback and is not guaranteed to work
permanently. Telegram may reject or rate-limit it with `API_ID_PUBLISHED_FLOOD`; the application
will not retry that condition automatically. It never downloads or scrapes API credentials.

Legacy `TG_API_ID`, `TG_API_HASH`, `TG_PHONE`, and `TG_SESSION_PATH` remain accepted temporarily
with a deprecation warning. Migrate to the `TELEGRAM_*` names above.

## Ubuntu Server Setup

Ubuntu 24.04 LTS is recommended because the application requires Python 3.12 or newer. From the
project root, bootstrap the management command and install the production service with:

```bash
sudo install -o root -g root -m 0755 scripts/ubuntu/tgs.py /usr/local/bin/tgs
sudo tgs install .
sudoedit /etc/sajadbot/sajadbot.env
```

Set the `TELEGRAM_*` values, then perform Telegram login in an interactive terminal and allow at
least one destination:

```bash
sudo -u sajadbot /usr/local/bin/sajadbot auth login
sudo -u sajadbot /usr/local/bin/sajadbot auth status
sudo -u sajadbot /usr/local/bin/sajadbot groups refresh
sudo -u sajadbot /usr/local/bin/sajadbot groups list
sudo -u sajadbot /usr/local/bin/sajadbot groups allow -1001234567890
```

Start the service after authentication succeeds:

```bash
sudo tgs enable --now
sudo tgs status
sudo tgs logs --follow
```

The installer keeps configuration in `/etc/sajadbot/sajadbot.env`, persistent state under
`/var/lib/sajadbot`, logs under `/var/log/sajadbot`, and application code under `/opt/sajadbot`.
It does not open or require an inbound network port. Re-run the installer from a newer project copy
to update the application, then restart it with `sudo systemctl restart sajadbot`.

On Ubuntu, use `sudo -u sajadbot /usr/local/bin/sajadbot <command>` in place of
`python run.py <command>` for administrative CLI commands.

### Ubuntu management with `tgs`

`tgs` manages the system installation while `sajadbot` remains the application CLI. Every command
and nested command supports both `-h` and `--help`:

```bash
tgs -h
tgs install -h
tgs update -h
tgs check -h
tgs auth login -h
```

Common operations:

```bash
# Install from a checkout, preserving the standard /opt, /etc, and /var layout
sudo tgs install /path/to/SajadBot

# Update from a newer checkout; an active service is stopped and started safely
sudo tgs update /path/to/newer/SajadBot

# Check local installation/configuration without contacting Telegram
sudo tgs check

# Include the application's non-mutating Telegram/network doctor
sudo tgs check --online --stop-service

# Service lifecycle and logs
sudo tgs start
sudo tgs stop
sudo tgs restart
sudo tgs status
sudo tgs logs --follow

# Telegram authorization; safely stop/restart an active service around session access
sudo tgs auth login --stop-service
sudo tgs auth login --qr --stop-service
sudo tgs auth status --stop-service

# Installed versions
tgs version
```

`tgs update` installs from the checkout you provide; it never performs an implicit `git pull` and
does not replace `/etc/sajadbot/sajadbot.env`, sessions, the application database, uploads, or logs.

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

Set `DRY_RUN=true` to validate and record commands without calling Telegram send APIs. For an
end-to-end connection and target-resolution check that starts no worker and performs no mutation,
run `python -m app start --dry-run`.

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

Do not put secrets on command lines or in logs. `TELEGRAM_API_HASH`,
`TELEGRAM_PUBLIC_API_HASH`, authorization codes, 2FA passwords, and session contents are
intentionally absent from application records.

## Troubleshooting

- **FloodWait / SlowMode:** inspect `status` and `queue list`. The worker waits until Telegram's
  specified time; do not restart repeatedly to bypass it.
- **Session unauthorized:** run `python -m app auth login`. Outgoing work stays stopped.
- **Session status:** run `python -m app auth status`; it never requests a login code.
- **Environment diagnostics:** run `python -m app doctor`; it never sends a message or login code.
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
