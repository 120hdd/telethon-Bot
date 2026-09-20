# SajadBot Project Conventions

## Architecture

- Python 3.12+, fully asynchronous application code.
- SQLite through a small `aiosqlite` repository layer; SQL is kept in `app/db`.
- Telegram API calls live in `app/telegram`; group delivery goes only through `TelegramSender`.
- The application database is the queue source of truth. Only one worker sends at a time.
- Core services accept protocols/fakes so tests never require Telegram credentials.

## Safety

- Store timestamps as UTC ISO-8601 strings and display them in the configured timezone.
- Never log secrets, message bodies, authorization codes, passwords, or session contents.
- Never auto-replay crash-interrupted `PROCESSING` jobs. Move them to `REVIEW_REQUIRED`.
- Never bypass Telegram waits or destination whitelisting.
- Keep the Telethon session and application database separate.

## Ubuntu Deployment

- Target Ubuntu 24.04 LTS and `systemd`; Python 3.12+ remains mandatory.
- Install application code read-only under `/opt/sajadbot` and run it as the unprivileged
  `sajadbot` system user.
- Keep configuration in `/etc/sajadbot/sajadbot.env`, persistent state in `/var/lib/sajadbot`,
  and logs in `/var/log/sajadbot`.
- Perform Telegram authentication interactively before enabling the service. Never place login
  codes or 2FA passwords in the environment file.

## Quality Gates

- Run `pytest` for behavioral changes.
- Run `ruff check .` and `ruff format --check .` before handoff.
- Add tests for every new job transition or Telegram error category.
