#!/usr/bin/env bash
# run from the mac: copies this repo to the pi and runs setup there.
#   ./pi/deploy.sh pi@raspberrypi.local
set -euo pipefail
pi_host="${1:?usage: pi/deploy.sh user@pi-host}"
repo_directory=$(cd "$(dirname "$0")/.." && pwd)
token="${LUNCH_LINE_INGEST_TOKEN:-$(op item get s5ty2d52vyc33b46kwfpi3kzfq --fields label=lunchline_ingest_token --reveal)}"

rsync -az --delete --exclude .venv --exclude .sanity --exclude __pycache__ --exclude .git --exclude web/node_modules \
  "$repo_directory/" "$pi_host:lunch_line_monitor/"
ssh -t "$pi_host" "cd lunch_line_monitor && LUNCH_LINE_INGEST_TOKEN='$token' ./pi/setup.sh"
