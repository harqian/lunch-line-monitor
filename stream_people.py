"""camera -> person detection -> live update on nuevasocratica.com/lunch-line-monitor.

  uv run stream_people.py --source arducam            # on the pi, arducam mini 2mp plus on spi + i2c
  uv run stream_people.py --source webcam --camera 0  # any opencv camera (how the pipeline was tested from a laptop)

reads the ingest token from the LUNCH_LINE_INGEST_TOKEN environment variable.
each update carries the jpeg frame plus the person boxes, drawn on the page.
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

import detect_people as detection

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


def build_update(frame, people, jpeg):
    frame_height, frame_width = frame.shape[:2]
    return {
        "captured_at": time.time(),
        "frame_width": frame_width,
        "frame_height": frame_height,
        "count": len(people),
        "jpeg": base64.b64encode(jpeg).decode("ascii"),
        "people": [{"box": [x1, y1, x2, y2], "confidence": round(confidence, 3)} for x1, y1, x2, y2, confidence in people],
    }


def run(source, model, feed, confidence_threshold, device, image_size, seconds_between_updates, annotated_path):
    consecutive_bad_frames = 0
    while True:
        started = time.monotonic()
        try:
            frame, jpeg = source.read_frame()
        except BadFrame as error:
            consecutive_bad_frames += 1
            print(f"bad frame {consecutive_bad_frames}/{MAX_CONSECUTIVE_BAD_FRAMES}: {error}", flush=True)
            if consecutive_bad_frames >= MAX_CONSECUTIVE_BAD_FRAMES:
                raise
            continue
        consecutive_bad_frames = 0
        people = detection.detect_people(model, frame, confidence_threshold, device, image_size)
        feed.send(build_update(frame, people, jpeg))
        if annotated_path:
            cv2.imwrite(annotated_path, detection.draw_people(frame, people))
        elapsed = time.monotonic() - started
        print(f"{len(people)} people ({elapsed:.2f}s)", flush=True)
        time.sleep(max(0.0, seconds_between_updates - elapsed))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", choices=["arducam", "webcam"], required=True)
    parser.add_argument("--camera", type=int, default=0, help="opencv camera index for --source webcam")
    parser.add_argument("--resolution", default="640x480", help="arducam jpeg size, e.g. 320x240, 640x480, 800x600")
    parser.add_argument("--model", default=str(detection.DEFAULT_MODEL_PATH))
    parser.add_argument("--confidence", type=float, default=0.4)
    parser.add_argument("--image-size", type=int, default=416, help="detection input size; 416 is ~0.42 s on a pi 4, 320 ~0.27 s, 640 ~0.9 s")
    parser.add_argument("--seconds-between-updates", type=float, default=0.0, help="0 = as fast as capture + detection allow")
    parser.add_argument("--save-annotated", help="overwrite this image with the latest annotated frame (for aiming the camera)")
    parser.add_argument("--url", default=INGEST_URL)
    arguments = parser.parse_args()

    token = os.environ["LUNCH_LINE_INGEST_TOKEN"]
    device = detection.pick_device()
    model = detection.load_model(arguments.model)
    source = ArducamSource(arguments.resolution) if arguments.source == "arducam" else WebcamSource(arguments.camera)
    run(source, model, LiveFeed(arguments.url, token), arguments.confidence, device, arguments.image_size, arguments.seconds_between_updates, arguments.save_annotated)


if __name__ == "__main__":
    main()
