"""feed hand-made people (points) through the real tracker + region counter, to check the per-region
counts, crossing rates and wait estimate without a camera or real people.

  uv run simulate_people.py                                        # interactive window (below)
  uv run simulate_people.py --script scenarios/one_walker.toml     # headless: play a scripted scenario
  ... --send                                                       # also push updates to the live page

interactive window: left-click to add a person, drag to walk them, right-click removes one.
  w / l   spawn a person walking in from the left / right edge at --walk-seconds per frame width
  c       clear everyone        r   reset the tracker + running averages        q / esc   quit

scripted scenario (toml, frame is 640x480, positions are box centers in pixels):
  seconds_per_update = 0.6            # how often the pipeline runs (the pi does ~0.6 s)
  [[people]]
  path = [[0, 0, 300], [7, 640, 300]] # [seconds, x, y] waypoints, straight lines between them;
                                      # in the scene from the first waypoint's time to the last's
  repeat_every = 4                    # optional: also start a copy every 4 s ...
  repeat_count = 10                   # ... this many copies in total
headless prints every update, then for each scripted person the time they actually spent in view
next to the wait the page showed when they appeared.

--send pushes each update to --url with LUNCH_LINE_INGEST_TOKEN, exactly like stream_people.py;
the default url is the real page, so only use it when nobody is relying on the live view.
"""
import argparse
import os
import time
import tomllib
from collections import deque

import cv2
import numpy

import detect_people as detection
import regions as region_counting
from track_people import PeopleTracker

FRAME_WIDTH, FRAME_HEIGHT = 640, 480
BOX_WIDTH, BOX_HEIGHT = 60, 180  # a standing person's box, centered on the point
BACKGROUND = (60, 60, 60)
POINT_COLOR = (200, 200, 200)
PICK_RADIUS = 30


def box_around(x, y):
    return (round(x - BOX_WIDTH / 2), round(y - BOX_HEIGHT / 2), round(x + BOX_WIDTH / 2), round(y + BOX_HEIGHT / 2), 1.0)


class Pipeline:
    """points -> PeopleTracker -> RegionCounter, the same steps stream_people.py runs on detections."""

    def __init__(self, config, feed=None):
        self.config = config
        self.feed = feed
        self.reset()

    def reset(self):
        self.tracker = PeopleTracker()
        self.region_counter = region_counting.RegionCounter.from_config(self.config)

    def step(self, points, now, background):
        """points: [(x, y)] in view this update. returns (tracked, snapshot)."""
        tracked = self.tracker.update([box_around(x, y) for x, y in points])
        snapshot = self.region_counter.update(tracked, FRAME_WIDTH, now)
        if self.feed is not None:
            from stream_people import build_update

            jpeg = cv2.imencode(".jpg", background, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
            self.feed.send(build_update(background, tracked, jpeg, snapshot))
        return tracked, snapshot


def annotate(background, tracked, snapshot):
    frame = background.copy()
    return detection.draw_people(frame, [person for _, person in tracked], person_ids=[person_id for person_id, _ in tracked], regions=snapshot)


def describe_seconds(seconds):
    return "?" if seconds is None else f"{seconds:.1f}s"


# --- scripted ---

def load_scenario(path):
    with open(path, "rb") as file:
        scenario = tomllib.load(file)
    paths = []
    for person in scenario["people"]:
        waypoints = [tuple(map(float, waypoint)) for waypoint in person["path"]]
        for copy in range(person.get("repeat_count", 1)):
            offset = copy * person.get("repeat_every", 0)
            paths.append([(seconds + offset, x, y) for seconds, x, y in waypoints])
    return scenario.get("seconds_per_update", 0.6), paths


def position_at(path, seconds):
    if not path[0][0] <= seconds <= path[-1][0]:
        return None
    for (start, x1, y1), (end, x2, y2) in zip(path, path[1:]):
        if start <= seconds <= end:
            fraction = 0.0 if end == start else (seconds - start) / (end - start)
            return x1 + (x2 - x1) * fraction, y1 + (y2 - y1) * fraction
    return path[-1][1:]


def in_frame(position):
    return position is not None and 0 <= position[0] < FRAME_WIDTH and 0 <= position[1] < FRAME_HEIGHT


def run_script(pipeline, scenario_path, annotated_path):
    seconds_per_update, paths = load_scenario(scenario_path)
    end = max(path[-1][0] for path in paths) + seconds_per_update
    first_seen, last_seen, wait_when_seen = {}, {}, {}
    background = numpy.full((FRAME_HEIGHT, FRAME_WIDTH, 3), BACKGROUND, numpy.uint8)
    now = 0.0
    while now <= end:
        visible = [(index, position_at(path, now)) for index, path in enumerate(paths)]
        visible = [(index, position) for index, position in visible if in_frame(position)]
        tracked, snapshot = pipeline.step([position for _, position in visible], now, background)
        for index, _ in visible:
            if index not in first_seen:
                first_seen[index], wait_when_seen[index] = now, snapshot.wait_seconds
            last_seen[index] = now
        print(
            f"t={now:6.1f}  ids {[person_id for person_id, _ in tracked]}  per region {snapshot.people}  "
            f"seconds per region {[describe_seconds(seconds) for seconds in snapshot.seconds_in_region]}  "
            f"wait {describe_seconds(snapshot.wait_seconds)} (pace {describe_seconds(snapshot.pace_wait_seconds)}, queue {describe_seconds(snapshot.queue_wait_seconds)}: {snapshot.queued_people} queued) {snapshot.line_direction or ''}",
            flush=True,
        )
        if annotated_path:
            cv2.imwrite(annotated_path, annotate(background, tracked, snapshot))
        now = round(now + seconds_per_update, 6)
    print("\nperson  appeared  actually in view  wait shown when they appeared")
    for index in sorted(first_seen):
        print(f"{index:6d}  {first_seen[index]:7.1f}s  {last_seen[index] - first_seen[index]:15.1f}s  {describe_seconds(wait_when_seen[index]):>8}")


# --- interactive ---

class Scene:
    """the people in the interactive window; each has a position, an optional walking velocity, and a trail."""

    def __init__(self, walk_seconds):
        self.walk_speed = FRAME_WIDTH / walk_seconds
        self.people = {}  # key -> {"x", "y", "velocity", "trail"}
        self.next_key = 1
        self.dragging = None

    def add(self, x, y, velocity=0.0):
        self.people[self.next_key] = {"x": float(x), "y": float(y), "velocity": velocity, "trail": deque(maxlen=40)}
        self.next_key += 1
        return self.next_key - 1

    def nearest(self, x, y):
        distances = [((person["x"] - x) ** 2 + (person["y"] - y) ** 2, key) for key, person in self.people.items()]
        distance, key = min(distances, default=(None, None))
        return key if distance is not None and distance <= PICK_RADIUS ** 2 else None

    def on_mouse(self, event, x, y, flags, _):
        if event == cv2.EVENT_LBUTTONDOWN:
            self.dragging = self.nearest(x, y) or self.add(x, y)
            self.people[self.dragging]["velocity"] = 0.0  # grabbing a walker takes over from auto-walk
        elif event == cv2.EVENT_MOUSEMOVE and self.dragging in self.people and flags & cv2.EVENT_FLAG_LBUTTON:
            self.people[self.dragging]["x"], self.people[self.dragging]["y"] = float(x), float(y)
        elif event == cv2.EVENT_LBUTTONUP:
            self.dragging = None
        elif event == cv2.EVENT_RBUTTONDOWN:
            self.people.pop(self.nearest(x, y), None)

    def walk(self, elapsed):
        for key, person in list(self.people.items()):
            person["x"] += person["velocity"] * elapsed
            if person["velocity"] and not 0 <= person["x"] < FRAME_WIDTH:
                del self.people[key]

    def points(self):
        return [(person["x"], person["y"]) for person in self.people.values() if in_frame((person["x"], person["y"]))]

    def draw(self, frame):
        for person in self.people.values():
            trail = numpy.array([*person["trail"], (person["x"], person["y"])], numpy.int32)
            cv2.polylines(frame, [trail], False, POINT_COLOR, 1)
            cv2.circle(frame, (round(person["x"]), round(person["y"])), 6, POINT_COLOR, -1)


def run_interactive(pipeline, seconds_per_update, walk_seconds, annotated_path):
    scene = Scene(walk_seconds)
    cv2.namedWindow("simulate people")
    cv2.setMouseCallback("simulate people", scene.on_mouse)
    tracked, snapshot = [], None
    last_tick = next_update = time.monotonic()
    while True:
        now = time.monotonic()
        scene.walk(now - last_tick)
        last_tick = now
        background = numpy.full((FRAME_HEIGHT, FRAME_WIDTH, 3), BACKGROUND, numpy.uint8)
        if now >= next_update:
            scene.draw(background)
            tracked, snapshot = pipeline.step(scene.points(), now, background)
            for person in scene.people.values():
                person["trail"].append((person["x"], person["y"]))
            print(f"ids {[person_id for person_id, _ in tracked]}  per region {snapshot.people}  wait {describe_seconds(snapshot.wait_seconds)} (pace {describe_seconds(snapshot.pace_wait_seconds)}, queue {describe_seconds(snapshot.queue_wait_seconds)}: {snapshot.queued_people} queued) {snapshot.line_direction or ''}", flush=True)
            if annotated_path:
                cv2.imwrite(annotated_path, annotate(background, tracked, snapshot))
            next_update = now + seconds_per_update
        # redraw every tick so dragging feels live; boxes and numbers change only on pipeline updates
        frame = numpy.full((FRAME_HEIGHT, FRAME_WIDTH, 3), BACKGROUND, numpy.uint8)
        scene.draw(frame)
        if snapshot is not None:
            frame = annotate(frame, tracked, snapshot)
        cv2.imshow("simulate people", frame)
        key = cv2.waitKey(15) & 0xFF
        if key in (ord("q"), 27):
            break
        if key == ord("w"):
            scene.add(0, FRAME_HEIGHT * 0.6, scene.walk_speed)
        elif key == ord("l"):
            scene.add(FRAME_WIDTH - 1, FRAME_HEIGHT * 0.6, -scene.walk_speed)
        elif key == ord("c"):
            scene.people.clear()
        elif key == ord("r"):
            pipeline.reset()
            print("reset tracker + running averages", flush=True)
    cv2.destroyAllWindows()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--script", help="play this scenario toml headless instead of opening a window")
    parser.add_argument("--config", default=str(region_counting.DEFAULT_CONFIG_PATH), help="region settings (toml)")
    parser.add_argument("--seconds-per-update", type=float, default=0.6, help="interactive: how often the pipeline runs (the pi does ~0.6 s)")
    parser.add_argument("--walk-seconds", type=float, default=7.0, help="interactive: w / l walkers take this long to cross the frame")
    parser.add_argument("--save-annotated", help="overwrite this image with the latest annotated frame")
    parser.add_argument("--send", action="store_true", help="also push each update to --url (needs LUNCH_LINE_INGEST_TOKEN)")
    parser.add_argument("--url", default="wss://nuevasocratica.com/lunch-line-monitor/ingest")
    arguments = parser.parse_args()

    feed = None
    if arguments.send:
        from stream_people import LiveFeed

        feed = LiveFeed(arguments.url, os.environ["LUNCH_LINE_INGEST_TOKEN"])
    pipeline = Pipeline(region_counting.load_config(arguments.config), feed)
    if arguments.script:
        run_script(pipeline, arguments.script, arguments.save_annotated)
    else:
        run_interactive(pipeline, arguments.seconds_per_update, arguments.walk_seconds, arguments.save_annotated)


if __name__ == "__main__":
    main()
