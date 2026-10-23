"""download every saved lunch line frame (one per camera update, ~1.6/s) from nuevasocratica.com/lunch-line-monitor.

  LUNCH_LINE_INGEST_TOKEN=... uv run download_frames.py --hours 24 --output-directory data/lunch-line

writes one jpeg per frame named by pacific time (to the millisecond) and people count, frames.jsonl with
each frame's boxes, and history.json (the ~2 s count history). frames already on disk are skipped.
"""
import argparse
import datetime
import json
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path
from zoneinfo import ZoneInfo

BASE_URL = "https://nuevasocratica.com/lunch-line-monitor"
PACIFIC = ZoneInfo("America/Los_Angeles")


def fetch(url, token=None):
    headers = {"User-Agent": "lunch-line-monitor-downloader"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as response:
        return response.read()


def list_frames(base_url, token, day):
    cursor = None
    while True:
        query = {"day": day.isoformat()} | ({"cursor": cursor} if cursor else {})
        page = json.loads(fetch(f"{base_url}/frames?{urllib.parse.urlencode(query)}", token))
        yield from page["frames"]
        cursor = page["cursor"]
        if cursor is None:
            return


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--base-url", default=BASE_URL)
    arguments = parser.parse_args()

    token = os.environ["LUNCH_LINE_INGEST_TOKEN"]
    cutoff_milliseconds = (time.time() - arguments.hours * 3600) * 1000
    output = arguments.output_directory
    output.mkdir(parents=True, exist_ok=True)
    history = fetch(f"{arguments.base_url}/history?hours={arguments.hours}&format=json")
    (output / "history.json").write_bytes(history)

    first_day = datetime.datetime.fromtimestamp(cutoff_milliseconds / 1000, PACIFIC).date()
    last_day = datetime.datetime.now(PACIFIC).date()
    downloaded = already_present = 0
    manifest = []
    for day_offset in range((last_day - first_day).days + 1):
        for frame in list_frames(arguments.base_url, token, first_day + datetime.timedelta(days=day_offset)):
            captured_milliseconds = int(frame["key"].split("/")[1].removesuffix(".jpg"))
            if captured_milliseconds < cutoff_milliseconds:
                continue
            captured = datetime.datetime.fromtimestamp(captured_milliseconds / 1000, PACIFIC)
            filename = f"{captured:%Y-%m-%d_%H-%M-%S}.{captured_milliseconds % 1000:03d}_{frame['count']}people.jpg"
            manifest.append({"file": filename, "pacific_time": captured.isoformat(timespec="milliseconds")} | frame)
            if (output / filename).exists():
                already_present += 1
                continue
            (output / filename).write_bytes(fetch(f"{arguments.base_url}/frames/{frame['key']}", token))
            downloaded += 1
    (output / "frames.jsonl").write_text("".join(json.dumps(entry) + "\n" for entry in manifest))
    print(f"{len(manifest)} frames in range: {downloaded} downloaded, {already_present} already present -> {output}")


if __name__ == "__main__":
    main()
