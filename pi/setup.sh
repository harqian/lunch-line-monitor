#!/usr/bin/env bash
# one-time setup, run ON the pi from the repo checkout:
#   LUNCH_LINE_INGEST_TOKEN=... ./pi/setup.sh
# enables spi + i2c, installs deps, installs a boot service. reboot once afterwards.
set -euo pipefail
repo_directory=$(cd "$(dirname "$0")/.." && pwd)
: "${LUNCH_LINE_INGEST_TOKEN:?set LUNCH_LINE_INGEST_TOKEN (1Password item 'Cloudflare API token', field lunchline_ingest_token)}"

sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0
sudo apt-get update
sudo apt-get install -y libgl1 libglib2.0-0  # opencv-python (pulled in by ultralytics) needs these on pi os lite
sudo usermod -aG spi,i2c "$USER"

command -v uv >/dev/null || curl -LsSf https://astral.sh/uv/install.sh | sh
uv_binary="$HOME/.local/bin/uv"
(cd "$repo_directory" && "$uv_binary" sync --frozen)

printf 'LUNCH_LINE_INGEST_TOKEN=%s\n' "$LUNCH_LINE_INGEST_TOKEN" | sudo tee /etc/lunch-line-monitor.env >/dev/null
sudo chmod 600 /etc/lunch-line-monitor.env
sed -e "s|__USER__|$USER|g" -e "s|__REPO__|$repo_directory|g" -e "s|__UV__|$uv_binary|g" \
  "$repo_directory/pi/lunch-line-monitor.service" | sudo tee /etc/systemd/system/lunch-line-monitor.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable lunch-line-monitor

echo "done. reboot, then check the camera with:"
echo "  cd $repo_directory && $uv_binary run python -m pi.arducam_mini_2mp_plus --selftest /tmp/arducam-test.jpg"
echo "and watch the service with: journalctl -u lunch-line-monitor -f"
