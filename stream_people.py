"""camera -> crowd count -> live update on nuevasocratica.com/lunch-line-monitor.

  uv run stream_people.py --source arducam            # on the pi, arducam mini 2mp plus on spi + i2c
  uv run stream_people.py --source webcam --camera 0  # any opencv camera (how the pipeline was tested from a laptop)

reads the ingest token from the LUNCH_LINE_INGEST_TOKEN environment variable.
each update carries the estimated count and a pixelated jpeg. the raw frame never leaves the pi.
the pixelated image goes out every --seconds-between-updates (1 s); the crowd count (~13 s of full cpu on the pi 4)
runs in a background thread with rests in between, so the pi stays below its thermal throttle (it sat at 82 c and had
throttled when counting nonstop, 11/16).
"""
import argparse
import base64
import json
import os
import threading
import time
from pathlib import Path

import cv2
import numpy
import torch
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

import crowd_count
import detect_people as detection  # webcam helpers only

INGEST_URL = "wss://nuevasocratica.com/lunch-line-monitor/ingest"
MAX_CONSECUTIVE_BAD_FRAMES = 10
CPU_TEMPERATURE_PATH = Path("/sys/class/thermal/thermal_zone0/temp")
# counting threads: 2 of the pi's 4 cores. 3 took the cpu from 70 to 82 c in one 12 s count (throttle starts at 80);
# fewer threads = less heat per second, and capture + pixelation keep a free core
COUNTING_THREADS = 2
# after each count, rest this many times as long as the count took (1.0 = counting half the time)
REST_PER_SECOND_COUNTING = 1.0
# the pi 4 throttles at 80 c and one count adds ~10 c, so a count only starts once the cpu is under this
START_COUNTING_BELOW_CELSIUS = 68.0
# a block of the 14 px grid counts as changed when its gray level moves this much (0-255); camera noise stays well under it
CHANGED_BLOCK_LEVELS = 12
# below this share of changed blocks the scene is the same (empty hall, night), so the last count is reused
CHANGED_BLOCK_SHARE_TO_RECOUNT = 0.005
# recount anyway after this long, so slow lighting drift can't freeze a wrong count
MAXIMUM_COUNT_AGE_SECONDS = 300


class WebcamSource:
    def __init__(self, camera_index):
        self.camera = detection.open_camera(camera_index)

    def read_frame(self):
        self.camera.grab()  # drop the frame opencv buffered while we slept, so we detect on a fresh one
        frame = detection.read_frame(self.camera)
        frame = cv2.resize(frame, (640, round(640 * frame.shape[0] / frame.shape[1])))
        return frame, cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()


class ArducamSource:
    def __init__(self, resolution):
        from pi import arducam_mini_2mp_plus  # spidev/smbus2 only exist on the pi

        self.driver = arducam_mini_2mp_plus
        self.camera = arducam_mini_2mp_plus.ArducamMini2MPPlus(resolution)

    def read_frame(self):
        try:
            jpeg = self.camera.capture_jpeg()
        except self.driver.CorruptCapture as error:
            raise BadFrame(str(error)) from error
        frame = cv2.imdecode(numpy.frombuffer(jpeg, numpy.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise BadFrame(f"arducam returned {len(jpeg)} bytes that opencv could not decode")
        return frame, jpeg


class BadFrame(Exception):
    pass


class LiveFeed:
    """one websocket to the site, reopened on the next update after any network failure."""

    def __init__(self, url, token):
        self.url = url
        self.token = token
        self.connection = None

    def send(self, update):
        try:
            if self.connection is None:
                self.connection = connect(self.url, max_size=None, additional_headers={"Authorization": f"Bearer {self.token}"}, open_timeout=10)
                print(f"connected to {self.url}", flush=True)
            self.connection.send(json.dumps(update))
        except InvalidStatus as error:
            if error.response.status_code in (401, 403):
                raise  # wrong token is a config error, not a network blip
            self.drop_connection(error)
        except (OSError, WebSocketException) as error:
            self.drop_connection(error)

    def drop_connection(self, error):
        print(f"live feed send failed ({error!r}); reconnecting on the next update", flush=True)
        if self.connection is not None:
            self.connection.close()
        self.connection = None


# the public page only ever gets this version: 14 px = a 45x34 grid on a 640x480 frame (was 20 until 11/16)
PIXEL_BLOCK_SIZE = 14


def block_grid(frame):
    frame_height, frame_width = frame.shape[:2]
    return cv2.resize(frame, (max(1, frame_width // PIXEL_BLOCK_SIZE), max(1, frame_height // PIXEL_BLOCK_SIZE)), interpolation=cv2.INTER_AREA)


def pixelated_jpeg(frame):
    frame_height, frame_width = frame.shape[:2]
    small = block_grid(frame)
    blocky = cv2.resize(small, (frame_width, frame_height), interpolation=cv2.INTER_NEAREST)
    return cv2.imencode(".jpg", blocky, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()


def cpu_temperature_celsius():
    """none off the pi (a laptop webcam run), where the page just hides the readout."""
    if not CPU_TEMPERATURE_PATH.exists():
        return None
    return int(CPU_TEMPERATURE_PATH.read_text()) / 1000


class BackgroundCounter:
    """counts the newest frame in its own thread, resting between counts and pausing when the cpu runs hot."""

    def __init__(self, counter):
        self.counter = counter
        self.lock = threading.Lock()
        self.newest_frame = None
        self.estimate = None
        self.counted_at = None
        self.counted_grid = None
        self.first_count_done = threading.Event()
        threading.Thread(target=self.count_forever, daemon=True).start()

    def offer(self, frame):
        with self.lock:
            self.newest_frame = frame

    def latest(self):
        with self.lock:
            return self.estimate, self.counted_at

    def changed_block_share(self, grid):
        """share of grid blocks that changed since the last count; 1.0 when there is none or it is too old."""
        if self.counted_grid is None or time.time() - self.counted_at > MAXIMUM_COUNT_AGE_SECONDS:
            return 1.0
        difference = cv2.absdiff(cv2.cvtColor(grid, cv2.COLOR_BGR2GRAY), cv2.cvtColor(self.counted_grid, cv2.COLOR_BGR2GRAY))
        return float((difference > CHANGED_BLOCK_LEVELS).mean())

    def wait_until_cool(self):
        temperature = cpu_temperature_celsius()
        while temperature is not None and temperature >= START_COUNTING_BELOW_CELSIUS:
            time.sleep(2)
            temperature = cpu_temperature_celsius()

    def count_forever(self):
        while True:
            with self.lock:
                frame = self.newest_frame
            if frame is None:
                time.sleep(0.2)
                continue
            grid = block_grid(frame)
            changed_share = self.changed_block_share(grid)
            if changed_share < CHANGED_BLOCK_SHARE_TO_RECOUNT:
                time.sleep(2)
                continue
            self.wait_until_cool()
            started = time.monotonic()
            estimate = crowd_count.count_people(self.counter, frame)
            seconds_counting = time.monotonic() - started
            with self.lock:
                self.estimate, self.counted_at, self.counted_grid = estimate, time.time(), grid
            self.first_count_done.set()
            print(f"{estimate:.1f} people estimated ({seconds_counting:.1f}s, {changed_share:.1%} of blocks changed, cpu {cpu_temperature_celsius()} c)", flush=True)
            time.sleep(seconds_counting * REST_PER_SECOND_COUNTING)


def build_update(frame, crowd_estimate, counted_at):
    frame_height, frame_width = frame.shape[:2]
    return {
        "captured_at": time.time(),
        "frame_width": frame_width,
        "frame_height": frame_height,
        "count": round(crowd_estimate),
        "counted_at": counted_at,  # the count lags the image: it comes from the newest frame the background counter finished
        "cpu_temperature_celsius": cpu_temperature_celsius(),
        # the raw frame never leaves the pi; the site only shows a jpeg marked with its pixel_block_size
        "jpeg": base64.b64encode(pixelated_jpeg(frame)).decode("ascii"),
        "pixel_block_size": PIXEL_BLOCK_SIZE,
        "people": [],  # no per-person boxes since 11/16: yolo only found unoccluded people and topped out ~10
    }


def run(source, background_counter, feed, seconds_between_updates, frame_path):
    consecutive_bad_frames = 0
    while True:
        started = time.monotonic()
        try:
            frame, _ = source.read_frame()
        except BadFrame as error:
            consecutive_bad_frames += 1
            print(f"bad frame {consecutive_bad_frames}/{MAX_CONSECUTIVE_BAD_FRAMES}: {error}", flush=True)
            if consecutive_bad_frames >= MAX_CONSECUTIVE_BAD_FRAMES:
                raise
            continue
        consecutive_bad_frames = 0
        background_counter.offer(frame)
        if frame_path:
            cv2.imwrite(frame_path, frame)
        # the worker requires a numeric count, so nothing goes out until the first count is in (~15 s after start)
        if background_counter.first_count_done.is_set():
            feed.send(build_update(frame, *background_counter.latest()))
        elapsed = time.monotonic() - started
        time.sleep(max(0.0, seconds_between_updates - elapsed))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=["arducam", "webcam"], required=True)
    parser.add_argument("--camera", type=int, default=0, help="opencv camera index for --source webcam")
    parser.add_argument("--resolution", default="640x480", help="arducam jpeg size, e.g. 320x240, 640x480, 800x600")
    parser.add_argument("--seconds-between-updates", type=float, default=1.0, help="how often the pixelated image goes out; counting runs on its own schedule")
    parser.add_argument("--save-annotated", help="overwrite this local image with the latest raw frame (for aiming the camera; never leaves the pi)")
    parser.add_argument("--url", default=INGEST_URL)
    arguments = parser.parse_args()

    token = os.environ["LUNCH_LINE_INGEST_TOKEN"]
    source = ArducamSource(arguments.resolution) if arguments.source == "arducam" else WebcamSource(arguments.camera)
    torch.set_num_threads(COUNTING_THREADS)
    run(source, BackgroundCounter(crowd_count.load_counter()), LiveFeed(arguments.url, token), arguments.seconds_between_updates, arguments.save_annotated)


if __name__ == "__main__":
    main()
