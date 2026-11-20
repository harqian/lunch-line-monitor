"""object permanence: gives each detected person an id that follows them from frame to frame.

each frame, every (track, detection) pair gets a cost: how far the box center moved, in units of
box height, plus how much the box changed size. pairs are matched cheapest-first, so each person
goes to whichever detection differs least from where they were last seen. a pair costing more than
max_cost is never matched (too far to be the same person).

  - unmatched detection -> a new person with a fresh id
  - unmatched track     -> kept, hidden, for max_missed_frames (covers a missed detection or a
                           brief occlusion), then forgotten; if they reappear near their last box
                           in time, they keep their id
"""
import colorsys
import math
from dataclasses import dataclass


@dataclass
class Track:
    person_id: int
    box: tuple  # (x1, y1, x2, y2)
    confidence: float
    missed_frames: int = 0


def match_cost(old_box, new_box):
    old_x1, old_y1, old_x2, old_y2 = old_box
    new_x1, new_y1, new_x2, new_y2 = new_box
    old_width, old_height = max(old_x2 - old_x1, 1), max(old_y2 - old_y1, 1)
    new_width, new_height = max(new_x2 - new_x1, 1), max(new_y2 - new_y1, 1)
    center_shift = math.hypot((new_x1 + new_x2 - old_x1 - old_x2) / 2, (new_y1 + new_y2 - old_y1 - old_y2) / 2)
    movement = center_shift / max(old_height, new_height)
    size_change = abs(math.log((new_width * new_height) / (old_width * old_height)))
    return movement + 0.5 * size_change


class PeopleTracker:
    def __init__(self, max_cost=1.0, max_missed_frames=4):
        self.max_cost = max_cost
        self.max_missed_frames = max_missed_frames
        self.tracks = []
        self.next_person_id = 1

    def update(self, people):
        """people: [(x1, y1, x2, y2, confidence)] from detect_people.
        returns the people visible this frame as [(person_id, (x1, y1, x2, y2, confidence))]."""
        candidate_pairs = sorted(
            (match_cost(track.box, person[:4]), track_index, person_index)
            for track_index, track in enumerate(self.tracks)
            for person_index, person in enumerate(people)
        )
        matched_tracks, matched_people = set(), set()
        visible = []
        for cost, track_index, person_index in candidate_pairs:
            if cost > self.max_cost:
                break
            if track_index in matched_tracks or person_index in matched_people:
                continue
            matched_tracks.add(track_index)
            matched_people.add(person_index)
            track = self.tracks[track_index]
            track.box, track.confidence, track.missed_frames = tuple(people[person_index][:4]), people[person_index][4], 0
            visible.append((track.person_id, people[person_index]))

        for track_index, track in enumerate(self.tracks):
            if track_index not in matched_tracks:
                track.missed_frames += 1
        self.tracks = [track for track in self.tracks if track.missed_frames <= self.max_missed_frames]

        for person_index, person in enumerate(people):
            if person_index not in matched_people:
                self.tracks.append(Track(self.next_person_id, tuple(person[:4]), person[4]))
                visible.append((self.next_person_id, person))
                self.next_person_id += 1

        return sorted(visible, key=lambda item: item[0])


def person_color_bgr(person_id):
    """golden-angle hue spacing so neighbouring ids look different; page.html uses the same formula."""
    hue = (person_id * 137.508) % 360
    red, green, blue = colorsys.hsv_to_rgb(hue / 360, 0.75, 1.0)
    return (round(blue * 255), round(green * 255), round(red * 255))
