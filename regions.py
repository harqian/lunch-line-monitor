"""splits the frame into equal-width vertical regions: how many people are centered in each, how often
people cross each boundary between neighbouring regions, and from that how fast the crowd moves and
how long a line takes.

a person belongs to the region holding the x of their box center. a tracked person only changes
region once their center is past the boundary by `hysteresis` of a region width, so someone standing
on a boundary doesn't cross it every frame. a crossing is a tracked person (same id from
track_people.PeopleTracker) changing region; jumping two regions in one update crosses both
boundaries in between. boundary i sits between region i and region i + 1; "rightward" is toward
higher regions in the camera image.

everything is a running average, updated every frame: exponentially weighted with time constant
`smoothing_seconds`, so recent movement counts most and old movement fades out smoothly instead of
falling off the end of a window. crossing rates are bias-corrected for the first few minutes, so
they're usable from the first crossing on (noisy at first, settling within about smoothing_seconds).

times come from how fast the crowd actually moves, per region, so no single person has to be tracked
end to end (which a packed line breaks) and a lone walker reads as their walking time:
  - each frame, every tracked person seen before adds how far their box center moved (in region
    widths) since they were last seen to the region they're in, and counts as present there for that
    same time; a person's first sighting adds neither (no movement to measure yet), so standing time
    and moving distance always cover the same stretch
  - seconds_in_region = running-average people in it / running-average distance moved in it per
    second, in the line's direction (whichever way the crowd is net-moving). both averages use the
    same weights, so the ratio holds its value while a region is empty and updates as people pass
  - a region nobody has moved through yet borrows the speed of the whole line
  - a person is queued while their own pace (smoothed over PACE_SMOOTHING_SECONDS) would take longer
    than `queued_if_slower_than_seconds` to cross the frame. only people who aren't queued feed the
    region speeds, so those measure walking pace, and a minute of queue history can't hold the wait
    up after the line drains
  - pace_wait_seconds = sum of seconds_in_region over the regions nobody is queued in right now:
    the walking part of the trip (the queued stretch is covered by the queue term)
  - queue_wait_seconds = people queued right now / how fast the front serves people. the serving
    rate is crossings into the front region (the last one in the line's direction) per second,
    counted only while someone queued is waiting in it, so neither a trickle of walkers nor a line
    that hasn't reached the front yet can pass for a slow server
  - wait_seconds = pace + queue: walk to the line, plus get served behind everyone queued ahead.
    it reacts immediately when people start or stop queuing; with nobody queued it's the walking
    time across the whole frame
when the crowd stops moving the times grow; below MIN_REGIONS_PER_SECOND they're unknown.
(the first version used people in view / crossings per second everywhere; that only holds when a
queue is service-limited, and for a few people walking freely it gave the gap between arrivals.)
someone standing still in view who isn't in line (a bystander) counts as queued.
"""
import math
import tomllib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_CONFIG_PATH = Path(__file__).parent / "lunch_line.toml"
FORGET_PERSON_AFTER_SECONDS = 30.0  # the tracker never reuses an id, so this only bounds memory
MIN_REGIONS_PER_SECOND = 1 / 3600  # crowd speed below one region an hour counts as stopped
MIN_AVERAGE_PEOPLE = 1e-3  # a region this empty has no speed of its own yet
PACE_SMOOTHING_SECONDS = 3.0  # a person's own pace, for deciding whether they're queued


def load_config(path=DEFAULT_CONFIG_PATH):
    with open(path, "rb") as file:
        return tomllib.load(file)


def rounded(value, digits):
    return None if value is None else round(value, digits)


@dataclass
class RegionSnapshot:
    people: list  # people centered in each region right now, left to right
    rightward_per_minute: list  # running-average crossings per minute, one per boundary, left to right
    leftward_per_minute: list
    smoothing_seconds: float
    average_people: list  # running-average people per region
    seconds_in_region: list  # per region, None while the crowd is stopped / hasn't moved yet
    pace_wait_seconds: float | None  # time to walk the regions with nobody queued, at the recent pace of people not queued
    queued_people: int  # people in view right now moving slower than queued_if_slower_than_seconds
    served_per_minute: float | None  # crossings into the front region per minute while someone queued waited there
    queue_wait_seconds: float | None  # queued_people / served rate
    wait_seconds: float | None  # pace + queue: predicted time to get through for someone joining now
    line_direction: str | None  # "rightward" / "leftward", None with no net movement

    def to_update(self):
        return {
            "people": self.people,
            "rightward_per_minute": [round(rate, 2) for rate in self.rightward_per_minute],
            "leftward_per_minute": [round(rate, 2) for rate in self.leftward_per_minute],
            "smoothing_seconds": self.smoothing_seconds,
            "average_people": [round(people, 2) for people in self.average_people],
            "seconds_in_region": [rounded(seconds, 1) for seconds in self.seconds_in_region],
            "pace_wait_seconds": rounded(self.pace_wait_seconds, 1),
            "queued_people": self.queued_people,
            "served_per_minute": rounded(self.served_per_minute, 2),
            "queue_wait_seconds": rounded(self.queue_wait_seconds, 1),
            "wait_seconds": rounded(self.wait_seconds, 1),
            "line_direction": self.line_direction,
        }


def estimate_seconds(average_people, regions_moved_per_second):
    """see the module docstring. returns (seconds per region, total, direction); all None while stopped."""
    net_movement = sum(regions_moved_per_second)
    if abs(net_movement) < MIN_REGIONS_PER_SECOND:
        return [None] * len(average_people), None, None
    direction = 1 if net_movement > 0 else -1
    line_seconds_per_region = sum(average_people) / (direction * net_movement)
    seconds_in_region = []
    for people, moved in zip(average_people, regions_moved_per_second):
        if people < MIN_AVERAGE_PEOPLE:
            seconds_in_region.append(line_seconds_per_region)
        elif direction * moved < people * MIN_REGIONS_PER_SECOND:
            seconds_in_region.append(None)  # people here, but not moving forward: stopped
        else:
            seconds_in_region.append(people / (direction * moved))
    total = None if None in seconds_in_region else sum(seconds_in_region)
    return seconds_in_region, total, "rightward" if direction > 0 else "leftward"


@dataclass
class PersonState:
    region: int
    position: float  # box center x in region widths
    first_seen: float
    last_seen: float
    pace: float = 0.0  # regions per second, signed, running average over PACE_SMOOTHING_SECONDS (starts at 0)

    def smoothed_pace(self, now):
        watched_weight = 1 - math.exp(-(now - self.first_seen) / PACE_SMOOTHING_SECONDS)
        return None if watched_weight <= 0 else self.pace / watched_weight


class RegionCounter:
    def __init__(self, region_count, smoothing_seconds=60.0, hysteresis=0.1, queued_if_slower_than_seconds=20.0):
        if region_count < 1:
            raise ValueError(f"need at least one region, got {region_count}")
        self.region_count = region_count
        self.smoothing_seconds = float(smoothing_seconds)
        self.hysteresis = hysteresis
        self.queued_if_slower_than_seconds = queued_if_slower_than_seconds
        self.person_states = {}  # person_id -> PersonState
        # running averages; crossing rates are in crossings per second and start at 0 (bias-corrected on read).
        # average_people and regions_moved_per_second also start at 0, but share weights, so their ratio needs no correction
        self.rightward_rate = [0.0] * (region_count - 1)
        self.leftward_rate = [0.0] * (region_count - 1)
        self.average_people = [0.0] * region_count
        self.regions_moved_per_second = [0.0] * region_count  # signed, + is rightward
        # crossings into the front region and time, both only while someone queued waits in it (shared weights again);
        # kept for both ends since the line's direction is only known afterwards
        self.served_rightward_while_queued = 0.0
        self.served_leftward_while_queued = 0.0
        self.queued_time_rightward = 0.0
        self.queued_time_leftward = 0.0
        self.started_at = None
        self.last_update_at = None

    @classmethod
    def from_config(cls, config):
        regions = config["regions"]
        return cls(regions["count"], regions.get("smoothing_seconds", 60.0), regions.get("hysteresis", 0.1), regions.get("queued_if_slower_than_seconds", 20.0))

    def region_of(self, center_x, frame_width, current_region=None):
        position = center_x * self.region_count / frame_width  # in region widths from the left edge
        region = min(max(int(position), 0), self.region_count - 1)
        if current_region is not None and current_region - self.hysteresis <= position < current_region + 1 + self.hysteresis:
            return current_region
        return region

    def update(self, tracked_people, frame_width, now):
        """tracked_people: [(person_id, (x1, y1, x2, y2, confidence))] from PeopleTracker.update.
        now: seconds on a monotonic clock."""
        people_per_region = [0] * self.region_count
        regions_moved = [0.0] * self.region_count
        seconds_present = [0.0] * self.region_count  # person-seconds over which regions_moved was measured
        rightward_crossings, leftward_crossings = [0] * (self.region_count - 1), [0] * (self.region_count - 1)
        queued_people = 0
        queued_per_region = [0] * self.region_count
        for person_id, (x1, _, x2, _, _) in tracked_people:
            center_x = (x1 + x2) / 2
            position = center_x * self.region_count / frame_width
            state = self.person_states.get(person_id)
            if state is None:
                self.person_states[person_id] = PersonState(self.region_of(center_x, frame_width), position, now, now)
                people_per_region[self.person_states[person_id].region] += 1
                continue  # nothing to measure on a first sighting
            region = self.region_of(center_x, frame_width, state.region)
            moved, seconds = position - state.position, now - state.last_seen
            state.pace = state.pace * math.exp(-seconds / PACE_SMOOTHING_SECONDS) + moved / PACE_SMOOTHING_SECONDS
            pace = state.smoothed_pace(now)
            if pace is not None and abs(pace) * self.queued_if_slower_than_seconds < self.region_count:
                queued_people += 1  # covered by the queue term, so kept out of the walking pace
                queued_per_region[region] += 1
            else:
                regions_moved[region] += moved
                seconds_present[region] += seconds
            if region != state.region:
                crossings = rightward_crossings if region > state.region else leftward_crossings
                for boundary in range(min(region, state.region), max(region, state.region)):
                    crossings[boundary] += 1
            state.region, state.position, state.last_seen = region, position, now
            people_per_region[region] += 1
        self.person_states = {
            person_id: state for person_id, state in self.person_states.items() if now - state.last_seen <= FORGET_PERSON_AFTER_SECONDS
        }

        if self.started_at is None:
            self.started_at = self.last_update_at = now
        elapsed = now - self.last_update_at
        self.last_update_at = now
        # an event rate decays by exp(-dt/tau) and each event adds 1/tau: the exponentially weighted crossings per second
        decay = math.exp(-elapsed / self.smoothing_seconds)
        self.rightward_rate = [rate * decay + count / self.smoothing_seconds for rate, count in zip(self.rightward_rate, rightward_crossings)]
        self.leftward_rate = [rate * decay + count / self.smoothing_seconds for rate, count in zip(self.leftward_rate, leftward_crossings)]
        # person-seconds present and the distance moved over them, with the same weights, so the ratio is seconds per region
        self.average_people = [average * decay + seconds / self.smoothing_seconds for average, seconds in zip(self.average_people, seconds_present)]
        self.regions_moved_per_second = [rate * decay + moved / self.smoothing_seconds for rate, moved in zip(self.regions_moved_per_second, regions_moved)]
        if self.region_count > 1:
            waiting_at_right, waiting_at_left = queued_per_region[-1] > 0, queued_per_region[0] > 0
            self.queued_time_rightward = self.queued_time_rightward * decay + (elapsed if waiting_at_right else 0.0) / self.smoothing_seconds
            self.queued_time_leftward = self.queued_time_leftward * decay + (elapsed if waiting_at_left else 0.0) / self.smoothing_seconds
            self.served_rightward_while_queued = self.served_rightward_while_queued * decay + (rightward_crossings[-1] if waiting_at_right else 0) / self.smoothing_seconds
            self.served_leftward_while_queued = self.served_leftward_while_queued * decay + (leftward_crossings[0] if waiting_at_left else 0) / self.smoothing_seconds

        # the rates started at 0, not at the true rate; dividing by the weight watched so far removes that pull toward 0
        watched_weight = 1 - math.exp(-(now - self.started_at) / self.smoothing_seconds)
        correction = 1 / watched_weight if watched_weight > 0 else 0.0
        rightward = [rate * correction for rate in self.rightward_rate]
        leftward = [rate * correction for rate in self.leftward_rate]
        # the shared weights cancel in the ratio, so this needs no bias correction
        seconds_in_region, _, line_direction = estimate_seconds(self.average_people, self.regions_moved_per_second)
        walking_regions = [seconds for seconds, queued_here in zip(seconds_in_region, queued_per_region) if not queued_here]
        pace_wait_seconds = None if line_direction is None or None in walking_regions else sum(walking_regions)
        average_people = [people * correction for people in self.average_people]
        served_per_second = queue_wait_seconds = None
        queued_time = self.queued_time_rightward if line_direction == "rightward" else self.queued_time_leftward
        if line_direction is not None and queued_time > 0:
            served = self.served_rightward_while_queued if line_direction == "rightward" else self.served_leftward_while_queued
            served_per_second = served / queued_time
            if served_per_second > 0:
                queue_wait_seconds = queued_people / served_per_second
        wait_seconds = None if pace_wait_seconds is None else pace_wait_seconds + (queue_wait_seconds or 0.0)
        return RegionSnapshot(
            people_per_region,
            [rate * 60 for rate in rightward],
            [rate * 60 for rate in leftward],
            self.smoothing_seconds,
            average_people,
            seconds_in_region,
            pace_wait_seconds,
            queued_people,
            None if served_per_second is None else served_per_second * 60,
            queue_wait_seconds,
            wait_seconds,
            line_direction,
        )
