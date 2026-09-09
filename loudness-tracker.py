#!/usr/bin/python3
"""Tell the loudness compensator what level the speakers are playing at.

The ear loses bass as the level drops, so quiet music sounds thin.  The
compensator in the filter graph can undo that, but only if it is told the
listening level, and only the output device knows it.  This watches the
volume and passes it on.

It writes nothing but the compensator's own controls, so a failure here can
change the loudness balance but never the calibration.  On the way out it
switches the compensation off, which is the safe state: leaving a low-volume
contour applied while the volume is high would be heard as far too much bass.
"""

import importlib.util
import signal
import subprocess
import sys
import time
from pathlib import Path

HELPER = Path(__file__).resolve().parent / "speaker-calibrate.py"
# Below this the change is inaudible and not worth a round of control writes.
VOLUME_EPSILON_DB = 0.4
RETRY_SECONDS = 2.0


def load_helper():
    spec = importlib.util.spec_from_file_location("speaker_calibrate", HELPER)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(HELPER.parent))
    spec.loader.exec_module(module)
    return module


class Tracker:
    def __init__(self, helper):
        self.helper = helper
        self.applied_db = None
        self.running = True

    def stop(self, *_):
        self.running = False

    def apply(self, volume_db, enabled=True):
        """Push one volume to the compensator; True when it was accepted."""
        node = self.helper.tuning_node_id()
        if node is None:
            return False
        live = self.helper.live_controls(node)
        if "loudcomp:volume" not in live:
            # A graph from before the compensator existed.  Nothing to drive,
            # and nothing worth failing over.
            return False
        controls = self.helper.loudness_controls(volume_db, enabled)
        if not self.helper.apply_controls_live(controls):
            return False
        self.applied_db = volume_db if enabled else None
        return True

    def follow(self):
        """Apply the volume now and again whenever the output changes."""
        while self.running:
            volume = self.helper.sink_volume_db(self.helper.VIRTUAL_SINK)
            if self.applied_db is None or abs(volume - self.applied_db) >= VOLUME_EPSILON_DB:
                if not self.apply(volume):
                    time.sleep(RETRY_SECONDS)
                    continue
            if not self.wait_for_change():
                time.sleep(RETRY_SECONDS)

    def wait_for_change(self):
        """Block until PipeWire reports a sink change; False if that failed."""
        try:
            events = subprocess.Popen(
                ["pactl", "subscribe"], stdout=subprocess.PIPE, text=True
            )
        except OSError:
            return False
        try:
            for line in events.stdout:
                if not self.running:
                    return True
                if "on sink" in line:
                    return True
            return False
        finally:
            events.terminate()
            try:
                events.wait(timeout=2)
            except subprocess.TimeoutExpired:
                events.kill()


def main():
    helper = load_helper()
    tracker = Tracker(helper)
    signal.signal(signal.SIGTERM, tracker.stop)
    signal.signal(signal.SIGINT, tracker.stop)
    try:
        tracker.follow()
    finally:
        # Whatever went wrong, do not leave a quiet-level contour running.
        tracker.apply(0.0, enabled=False)


if __name__ == "__main__":
    main()
