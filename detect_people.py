"""webcam person detector: draws a bounding box around every human in frame.

  uv run detect_people.py                         # live window, q or esc to quit
  uv run detect_people.py --snapshot out.png      # headless: grab frames, save annotated image, print count
  uv run detect_people.py --image photo.jpg --snapshot out.png
"""
import argparse
import time
from pathlib import Path

import cv2
import torch
from ultralytics import YOLO

import regions as region_counting
from track_people import PeopleTracker, person_color_bgr

PERSON_CLASS_ID = 0  # coco class 0 = person
BOX_COLOR = (0, 255, 0)
REGION_COLOR = (255, 255, 255)
DEFAULT_MODEL_PATH = Path(__file__).parent / "models" / "yolo26n.pt"  # auto-downloads on first run


def pick_device():
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


def load_model(model_name):
    return YOLO(model_name)


def detect_people(model, frame, confidence_threshold, device, image_size=640):
    """returns a list of (x1, y1, x2, y2, confidence) in pixel coords."""
    result = model.predict(frame, classes=[PERSON_CLASS_ID], conf=confidence_threshold, device=device, imgsz=image_size, verbose=False)[0]
    corners = result.boxes.xyxy.cpu().numpy()
    confidences = result.boxes.conf.cpu().numpy()
    return [(int(x1), int(y1), int(x2), int(y2), float(confidence)) for (x1, y1, x2, y2), confidence in zip(corners, confidences)]


def put_backed_text(frame, text, center_x, baseline_y, scale, color):
    """text centered on center_x, on a dark box so it reads on any background."""
    (width, height), descent = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, scale, 1)
    left = round(center_x - width / 2)
    cv2.rectangle(frame, (left - 3, baseline_y - height - 3), (left + width + 3, baseline_y + descent), (0, 0, 0), -1)
    cv2.putText(frame, text, (left, baseline_y), cv2.FONT_HERSHEY_SIMPLEX, scale, color, 1)


def draw_regions(frame, snapshot):
    """region boundaries, people per region along the bottom with estimated seconds spent in it above,
    crossings per minute at each boundary (">" rightward, "<" leftward), and in the top right the
    predicted wait for someone joining now (walking time + time to be served behind everyone queued)."""
    height, width = frame.shape[:2]
    region_count = len(snapshot.people)
    for boundary in range(1, region_count):
        x = round(boundary * width / region_count)
        cv2.line(frame, (x, 0), (x, height), REGION_COLOR, 1)
        put_backed_text(frame, f">{snapshot.rightward_per_minute[boundary - 1]:.1f}", x, height - 36, 0.45, REGION_COLOR)
        put_backed_text(frame, f"<{snapshot.leftward_per_minute[boundary - 1]:.1f}", x, height - 14, 0.45, REGION_COLOR)
    for region, people_in_region in enumerate(snapshot.people):
        center_x = round((region + 0.5) * width / region_count)
        put_backed_text(frame, str(people_in_region), center_x, height - 14, 0.9, REGION_COLOR)
        seconds = snapshot.seconds_in_region[region]
        put_backed_text(frame, "?s" if seconds is None else f"{seconds:.0f}s", center_x, height - 48, 0.45, REGION_COLOR)
    def describe(seconds):
        return "?" if seconds is None else f"{seconds:.0f}s"

    put_backed_text(frame, f"wait now ~{describe(snapshot.wait_seconds)} {snapshot.line_direction or ''}".rstrip(), width - 110, 20, 0.45, REGION_COLOR)
    put_backed_text(frame, f"walk {describe(snapshot.pace_wait_seconds)} + queue {describe(snapshot.queue_wait_seconds)} ({snapshot.queued_people} queued)", width - 110, 42, 0.45, REGION_COLOR)
    put_backed_text(frame, f"crossings/min, ~last {snapshot.smoothing_seconds:.0f}s", width - 110, 64, 0.45, REGION_COLOR)


def draw_people(frame, people, frames_per_second=None, person_ids=None, regions=None):
    """person_ids (from track_people.PeopleTracker), when given, colors and labels each box by id.
    regions (a regions.RegionSnapshot), when given, is drawn under the boxes."""
    if regions is not None:
        draw_regions(frame, regions)
    for index, (x1, y1, x2, y2, confidence) in enumerate(people):
        color, label = BOX_COLOR, f"person {confidence:.2f}"
        if person_ids is not None:
            color, label = person_color_bgr(person_ids[index]), f"#{person_ids[index]} {confidence:.2f}"
        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
        cv2.putText(frame, label, (x1, max(y1 - 8, 16)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
    header = f"people: {len(people)}"
    if frames_per_second is not None:
        header += f"   {frames_per_second:.0f} fps"
    cv2.putText(frame, header, (12, 32), cv2.FONT_HERSHEY_SIMPLEX, 1.0, BOX_COLOR, 2)
    return frame


def open_camera(camera_index):
    camera = cv2.VideoCapture(camera_index)
    if not camera.isOpened():
        raise RuntimeError(f"could not open camera {camera_index} (check macOS camera permission for your terminal app)")
    return camera


def read_frame(camera):
    ok, frame = camera.read()
    if not ok:
        raise RuntimeError("camera returned no frame")
    return frame


def run_live(model, camera_index, confidence_threshold, device, region_counter):
    camera = open_camera(camera_index)
    tracker = PeopleTracker()
    smoothed_frames_per_second = None
    try:
        while True:
            started = time.perf_counter()
            frame = read_frame(camera)
            tracked = tracker.update(detect_people(model, frame, confidence_threshold, device))
            region_snapshot = region_counter.update(tracked, frame.shape[1], time.monotonic())
            instant_frames_per_second = 1.0 / (time.perf_counter() - started)
            smoothed_frames_per_second = instant_frames_per_second if smoothed_frames_per_second is None else 0.9 * smoothed_frames_per_second + 0.1 * instant_frames_per_second
            people = [person for _, person in tracked]
            person_ids = [person_id for person_id, _ in tracked]
            cv2.imshow("people", draw_people(frame, people, smoothed_frames_per_second, person_ids, region_snapshot))
            if cv2.waitKey(1) & 0xFF in (ord("q"), 27):
                break
    finally:
        camera.release()
        cv2.destroyAllWindows()


def run_snapshot(model, camera_index, confidence_threshold, device, output_path, warmup_frames):
    """headless check: skip warmup frames (auto-exposure settles), annotate one, save it."""
    camera = open_camera(camera_index)
    try:
        for _ in range(warmup_frames):
            read_frame(camera)
        frame = read_frame(camera)
    finally:
        camera.release()
    return annotate_and_save(model, frame, confidence_threshold, device, output_path)


def annotate_and_save(model, frame, confidence_threshold, device, output_path):
    people = detect_people(model, frame, confidence_threshold, device)
    cv2.imwrite(output_path, draw_people(frame, people))
    print(f"{len(people)} people -> {output_path}")
    for x1, y1, x2, y2, confidence in people:
        print(f"  box ({x1},{y1})-({x2},{y2}) confidence {confidence:.2f}")
    return people


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--camera", type=int, default=0)
    parser.add_argument("--model", default=str(DEFAULT_MODEL_PATH), help="any ultralytics detection weights")
    parser.add_argument("--confidence", type=float, default=0.4)
    parser.add_argument("--snapshot", help="headless: save one annotated frame here instead of opening a window")
    parser.add_argument("--image", help="run on this image file instead of the webcam (needs --snapshot)")
    parser.add_argument("--warmup-frames", type=int, default=15)
    parser.add_argument("--config", default=str(region_counting.DEFAULT_CONFIG_PATH), help="region settings for the live window")
    arguments = parser.parse_args()
    if arguments.image and not arguments.snapshot:
        parser.error("--image needs --snapshot")

    device = pick_device()
    model = load_model(arguments.model)
    if arguments.image:
        frame = cv2.imread(arguments.image)
        if frame is None:
            raise FileNotFoundError(arguments.image)
        annotate_and_save(model, frame, arguments.confidence, device, arguments.snapshot)
    elif arguments.snapshot:
        run_snapshot(model, arguments.camera, arguments.confidence, device, arguments.snapshot, arguments.warmup_frames)
    else:
        run_live(model, arguments.camera, arguments.confidence, device, region_counting.RegionCounter.from_config(region_counting.load_config(arguments.config)))


if __name__ == "__main__":
    main()
