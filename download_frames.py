"""download the saved lunch line frames (one per ~10 s) plus every count/box row from nuevasocratica.com/lunch-line-monitor.

  LUNCH_LINE_INGEST_TOKEN=... uv run download_frames.py --hours 24 --output-directory data/lunch-line

writes history.json (every ~2 s row, with boxes) and one jpeg per saved frame, named by pacific time and count.
already-downloaded frames are skipped, so rerunning only fetches new ones.
"""
import argparse
import json
import os
import urllib.request
from pathlib import Path

BASE_URL = "https://nuevasocratica.com/lunch-line-monitor"


def fetch(url, token=None):
    headers = {"User-Agent": "lunch-line-monitor-downloader"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
        return response.read()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, required=True)
    parser.add_argument("--output-directory", type=Path, required=True)
    parser.add_argument("--base-url", default=BASE_URL)
    arguments = parser.parse_args()

    token = os.environ["LUNCH_LINE_INGEST_TOKEN"]
    rows = json.loads(fetch(f"{arguments.base_url}/history?hours={arguments.hours}&format=json"))
    arguments.output_directory.mkdir(parents=True, exist_ok=True)
    (arguments.output_directory / "history.json").write_text(json.dumps(rows, indent=1))

    downloaded = already_present = 0
    for row in rows:
        if row["frame"] is None:
            continue
        timestamp = row["pacific_time"].replace(" ", "_").replace(":", "-")
        target = arguments.output_directory / f"{timestamp}_{row['count']}people.jpg"
        if target.exists():
            already_present += 1
            continue
        target.write_bytes(fetch(f"{arguments.base_url}/frames/{row['frame']}", token))
        downloaded += 1
    print(f"{len(rows)} history rows, {downloaded} frames downloaded, {already_present} already present -> {arguments.output_directory}")


if __name__ == "__main__":
    main()
