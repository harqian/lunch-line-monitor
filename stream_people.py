"""camera -> crowd count -> live update on nuevasocratica.com/lunch-line-monitor.

  uv run stream_people.py --source arducam            # on the pi, arducam mini 2mp plus on spi + i2c
  uv run stream_people.py --source webcam --camera 0  # any opencv camera (how the pipeline was tested from a laptop)

reads the ingest token from the LUNCH_LINE_INGEST_TOKEN environment variable.
each update carries the estimated count and a pixelated jpeg. the raw frame never leaves the pi.
"""
import argparse
import base64
import json
import os
import time

import cv2
import numpy
from websockets.exceptions import InvalidStatus, WebSocketException
from websockets.sync.client import connect

import crowd_count
import detect_people as detection  # webcam helpers only

INGEST_URL = "wss://nuevasocratica.com/lunch-line-monitor/ingest"
MAX_CONSECUTIVE_BAD_FRAMES = 10


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


def pixelated_jpeg(frame):
    frame_height, frame_width = frame.shape[:2]
    small = cv2.resize(frame, (max(1, frame_width // PIXEL_BLOCK_SIZE), max(1, frame_height // PIXEL_BLOCK_SIZE)), interpolation=cv2.INTER_AREA)
    blocky = cv2.resize(small, (frame_width, frame_height), interpolation=cv2.INTER_NEAREST)
    return cv2.imencode(".jpg", blocky, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()


def build_update(frame, crowd_estimate):
    frame_height, frame_width = frame.shape[:2]
    return {
        "captured_at": time.time(),
        "frame_width": frame_width,
        "frame_height": frame_height,
        "count": round(crowd_estimate),
        # the raw frame never leaves the pi; the site only shows a jpeg marked with its pixel_block_size
        "jpeg": base64.b64encode(pixelated_jpeg(frame)).decode("ascii"),
        "pixel_block_size": PIXEL_BLOCK_SIZE,
        "people": [],  # no per-person boxes since 11/16: yolo only found unoccluded people and topped out ~10
    }


def run(source, counter, feed, seconds_between_updates, frame_path):
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
        crowd_estimate = crowd_count.count_people(counter, frame)
        feed.send(build_update(frame, crowd_estimate))
        if frame_path:
            cv2.imwrite(frame_path, frame)
        elapsed = time.monotonic() - started
        print(f"{crowd_estimate:.1f} people estimated ({elapsed:.2f}s)", flush=True)
        time.sleep(max(0.0, seconds_between_updates - elapsed))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=["arducam", "webcam"], required=True)
    parser.add_argument("--camera", type=int, default=0, help="opencv camera index for --source webcam")
    parser.add_argument("--resolution", default="640x480", help="arducam jpeg size, e.g. 320x240, 640x480, 800x600")
    parser.add_argument("--seconds-between-updates", type=float, default=0.0, help="0 = as fast as capture + counting allow")
    parser.add_argument("--save-annotated", help="overwrite this local image with the latest raw frame (for aiming the camera; never leaves the pi)")
    parser.add_argument("--url", default=INGEST_URL)
    arguments = parser.parse_args()

    token = os.environ["LUNCH_LINE_INGEST_TOKEN"]
    source = ArducamSource(arguments.resolution) if arguments.source == "arducam" else WebcamSource(arguments.camera)
    run(source, crowd_count.load_counter(), LiveFeed(arguments.url, token), arguments.seconds_between_updates, arguments.save_annotated)


if __name__ == "__main__":
    main()
