#!/usr/bin/env bash
set -Eeuo pipefail

readonly APP_USER="sajadbot"
readonly APP_GROUP="sajadbot"
readonly APP_DIR="/opt/sajadbot"
readonly STATE_DIR="/var/lib/sajadbot"
readonly LOG_DIR="/var/log/sajadbot"
readonly CONFIG_DIR="/etc/sajadbot"
readonly ENV_FILE="${CONFIG_DIR}/sajadbot.env"
readonly SERVICE_FILE="/etc/systemd/system/sajadbot.service"
readonly CLI_FILE="/usr/local/bin/sajadbot"
readonly TGS_FILE="/usr/local/bin/tgs"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly SCRIPT_DIR
SOURCE_DIR="$(cd -- "${SCRIPT_DIR}/../.." && pwd)"
readonly SOURCE_DIR

usage() {
  cat <<'EOF'
Install or update SajadBot as a systemd service on Ubuntu.

Usage:
  sudo bash scripts/ubuntu/install.sh

The installer does not start or enable the service on a first install. After it
finishes, edit /etc/sajadbot/sajadbot.env, authenticate interactively, and then
enable the service as shown in the printed instructions.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

if [[ $# -ne 0 ]]; then
  usage >&2
  exit 2
fi

if [[ ${EUID} -ne 0 ]]; then
  printf 'This installer must run as root. Use: sudo bash %s\n' "$0" >&2
  exit 1
fi

if [[ ! -r /etc/os-release ]]; then
  printf 'Cannot identify this operating system: /etc/os-release is missing.\n' >&2
  exit 1
fi

# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != "ubuntu" ]]; then
  printf 'This installer supports Ubuntu; detected: %s.\n' "${PRETTY_NAME:-unknown}" >&2
  exit 1
fi

printf 'Installing operating-system dependencies...\n'
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends ca-certificates python3 python3-pip python3-venv

PYTHON_BIN="${PYTHON_BIN:-python3}"
readonly PYTHON_BIN
if ! "${PYTHON_BIN}" - <<'PY'
import sys

if sys.version_info < (3, 12):
    raise SystemExit(1)
PY
then
  printf '%s must be Python 3.12 or newer. Ubuntu 24.04 LTS is recommended.\n' "${PYTHON_BIN}" >&2
  exit 1
fi

if ! getent group "${APP_GROUP}" >/dev/null; then
  groupadd --system "${APP_GROUP}"
fi

if ! id -u "${APP_USER}" >/dev/null 2>&1; then
  useradd \
    --system \
    --gid "${APP_GROUP}" \
    --home-dir "${STATE_DIR}" \
    --create-home \
    --shell /usr/sbin/nologin \
    "${APP_USER}"
fi

install -d -o root -g root -m 0755 "${APP_DIR}"
install -d -o "${APP_USER}" -g "${APP_GROUP}" -m 0700 \
  "${STATE_DIR}" "${STATE_DIR}/sessions" "${STATE_DIR}/uploads" "${LOG_DIR}"
install -d -o root -g "${APP_GROUP}" -m 0750 "${CONFIG_DIR}"

printf 'Copying application files to %s...\n' "${APP_DIR}"
install -d -o root -g root -m 0755 "${APP_DIR}/app"
install -d -o root -g root -m 0755 "${APP_DIR}/deploy/ubuntu" "${APP_DIR}/scripts/ubuntu"
if [[ "$(readlink -f "${SOURCE_DIR}")" != "$(readlink -f "${APP_DIR}")" ]]; then
  cp -a "${SOURCE_DIR}/app/." "${APP_DIR}/app/"
  cp -a "${SOURCE_DIR}/deploy/ubuntu/." "${APP_DIR}/deploy/ubuntu/"
  cp -a "${SOURCE_DIR}/scripts/ubuntu/." "${APP_DIR}/scripts/ubuntu/"
  install -o root -g root -m 0644 \
    "${SOURCE_DIR}/run.py" \
    "${SOURCE_DIR}/pyproject.toml" \
    "${SOURCE_DIR}/README.md" \
    "${SOURCE_DIR}/docs-fa.html" \
    "${APP_DIR}/"
else
  printf 'Source is already %s; keeping the application files in place.\n' "${APP_DIR}"
fi

printf 'Creating the Python virtual environment...\n'
if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  "${PYTHON_BIN}" -m venv "${APP_DIR}/.venv"
fi
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
"${APP_DIR}/.venv/bin/python" -m pip install --upgrade "${APP_DIR}"

if [[ ! -e "${ENV_FILE}" ]]; then
  cat >"${ENV_FILE}" <<'EOF'
TELEGRAM_PHONE=+10000000000
TELEGRAM_API_MODE=auto
TELEGRAM_API_ID=
TELEGRAM_API_HASH=
TELEGRAM_PUBLIC_API_ID=
TELEGRAM_PUBLIC_API_HASH=
TELEGRAM_SESSION_DIR=/var/lib/sajadbot/sessions
DATABASE_URL=sqlite+aiosqlite:////var/lib/sajadbot/app.db
UPLOADS_DIR=/var/lib/sajadbot/uploads
LOG_PATH=/var/log/sajadbot/app.log

LOG_LEVEL=INFO
LOG_FORMAT=console
LOG_MESSAGE_BODIES=false

CONTROL_SAVED_MESSAGES=true
DRY_RUN=false
LOCAL_TIMEZONE=Asia/Tehran

DEFAULT_SEND_INTERVAL_SECONDS=8
MAX_QUEUE_ATTEMPTS=5
MAX_AUTOMATIC_FLOOD_WAIT_SECONDS=300
WORKER_POLL_SECONDS=1
MAX_MEDIA_BYTES=2147483648
EOF
  chown root:"${APP_GROUP}" "${ENV_FILE}"
  chmod 0640 "${ENV_FILE}"
  printf 'Created %s.\n' "${ENV_FILE}"
else
  printf 'Keeping the existing configuration at %s.\n' "${ENV_FILE}"
fi

install -o root -g root -m 0644 \
  "${SOURCE_DIR}/deploy/ubuntu/sajadbot.service" "${SERVICE_FILE}"
install -o root -g root -m 0755 \
  "${SOURCE_DIR}/scripts/ubuntu/sajadbot" "${CLI_FILE}"
install -o root -g root -m 0755 \
  "${SOURCE_DIR}/scripts/ubuntu/tgs.py" "${TGS_FILE}"

systemctl daemon-reload

cat <<'EOF'

SajadBot is installed. Complete these steps:

  1. sudoedit /etc/sajadbot/sajadbot.env
  2. sudo tgs auth login
  3. sudo tgs auth status
  4. sudo -u sajadbot /usr/local/bin/sajadbot groups refresh
  5. sudo -u sajadbot /usr/local/bin/sajadbot groups allow GROUP_ID
  6. sudo tgs enable --now
  7. sudo tgs status

Follow logs with:
  sudo tgs logs --follow

See every management command with:
  tgs -h
EOF
