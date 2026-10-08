# lunch_line_monitor

camera -> person detection (YOLO26n) -> live count + box positions on https://nuevasocratica.com/lunch-line-monitor. target hardware: raspberry pi + arducam mini 2mp plus (OV2640, spi + i2c). no images leave the device, only `{count, people:[{box, confidence}], frame_width, frame_height}`.

## layout
- `detect_people.py`: mac webcam viewer + the shared detection functions (`load_model`, `detect_people`, `draw_people`).
- `stream_people.py`: capture -> detect -> websocket push. `--source arducam` on the pi, `--source webcam --camera N` anywhere (how the whole pipeline is tested without the pi).
- `pi/`: arducam driver (`arducam_mini_2mp_plus.py`, `ov2640_registers.py`), `setup.sh` (run on the pi), `deploy.sh user@host` (run from the mac: rsync + setup), systemd unit.
- `web/`: cloudflare worker `lunch-line-monitor` on route `nuevasocratica.com/lunch-line-monitor*` (account b5767c459ae1102e08c8dd559b76ee15). one durable object `LunchLine` fans updates out over hibernatable websockets: camera -> `/lunch-line-monitor/ingest` (Bearer INGEST_TOKEN), browsers -> `/lunch-line-monitor/live`. latest update is memory-only, so a redeploy shows "waiting for the camera" until the next update.

## run / verify
- mac end-to-end: `LUNCH_LINE_INGEST_TOKEN=$(op item get s5ty2d52vyc33b46kwfpi3kzfq --fields label=lunchline_ingest_token --reveal) uv run stream_people.py --source webcam --camera 1 --save-annotated .sanity/x.png`, then open the page. camera index moves around (iPhone continuity camera can take index 0 and return no frames); the MacBook camera was index 1 on 2026-10-08.
- deploy worker: `cd web && CLOUDFLARE_API_TOKEN=<1P 'Cloudflare API token' field full_token> CLOUDFLARE_ACCOUNT_ID=b5767c459ae1102e08c8dd559b76ee15 wrangler deploy`. `curl` websocket checks need `--http1.1` (over h2 the Upgrade header is dropped and the worker answers 426).
- pi: `./pi/deploy.sh user@host`, reboot, then `uv run python -m pi.arducam_mini_2mp_plus --selftest /tmp/arducam-test.jpg`, then `journalctl -u lunch-line-monitor -f`. `/tmp/lunch-line-latest.jpg` on the pi is the latest annotated frame, for aiming the camera.

## facts
- wiring (verified against arducam docs, 2026-10-08): CS 24 (CE0), MOSI 19, MISO 21, SCK 23, GND 6, SDA 3, SCL 5, **VCC on pin 1 (3.3V), not pin 2**: the board's level-shifter pull-ups likely go to VCC, which would put 5V on pi GPIO (no schematic found; arducam's own pi guide uses 3.3V).
- driver facts + sources: `.sanity/arducam_research.md` (not committed). spi mode 0 at 4 MHz (8 MHz unreliable in the field). whole frame read in one chip-select-low spidev transaction, which needs `spidev.bufsiz=262144` in cmdline.txt (setup.sh adds it; the driver fails loudly if it's missing). no fixed dummy byte on the plus; the jpeg is cut from FFD8 to the next FFD9. chunked reads (re-send 0x3C per chunk) are also endorsed by arducam staff if the bufsiz route ever fails.
- the driver has never touched real hardware. `.sanity/fake_arducam_test.py` runs it plus `--source arducam` against an emulated arduchip/ov2640 serving a real jpeg.
- 1 update/s keeps cloudflare's free tier: websocket messages bill 20:1, and a durable object awake 24h is ~11k of the 13k free GB-s/day. don't raise the rate without checking the plan.
- linux torch comes from the pytorch cpu index (`tool.uv.sources`); pypi's linux wheels drag in cuda. verified resolution for aarch64: torch 2.14.1+cpu, no nvidia libraries besides the pure-python nvidia-ml-py.
- measured 2026-10-08: ~0.12-0.15 s per frame end-to-end on the mac (mps).
- known weakness: tightly overlapping people (exactly a lunch line) can merge into one box -> undercount. bigger model (`--model models/yolo26s.pt`) or lower `--confidence` is the first lever.
- `.sanity/` = one-off verify artifacts (contains photos of people, never commit) + a camoufox file-driven browser repl.
