#!/bin/bash
# Start the mintbot in polling mode. Loads config (incl. BOT_TOKEN) from
# the .env file next to this script. Used by the systemd service and the
# watchdog cron — never put the token on the command line.
set -e
cd "$(dirname "$0")"
set -a
# shellcheck disable=SC1091
. ./.env
set +a
exec ./.venv/bin/python bot.py --poll
