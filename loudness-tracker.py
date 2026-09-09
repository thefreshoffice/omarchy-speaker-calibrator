#!/usr/bin/python3
"""Tell the loudness compensator what level the speakers are playing at.

The ear loses bass as the level drops, so quiet music sounds thin.  The
compensator in the filter graph can undo that, but only if it is told the
listening level, and only the output device knows it.  This watches the
volume and passes it on.

It holds one subscription open for the life of the service rather than
resubscribing after every change: restarting it each time leaves a window in
which a volume move is missed, and a missed move leaves the wrong contour
applied until the next one.

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
        self.node = None
        self.applied_db = None
        self.profile_stamp = None
        self.enabled = False
        self.running = True

    def stop(self, *_):
        self.running = False

    def find_node(self):
        """The running filter, or None.  Looked up once and kept."""
        if self.node is None:
            node = self.helper.tuning_node_id()
            if node is None:
                return None
            if "loudcomp:volume" not in self.helper.live_controls(node):
                # A graph from before the compensator existed: nothing to drive.
                return None
            self.node = node
        return self.node

    def apply(self, volume_db, enabled=True):
        """Push one volume to the compensator; True when it was accepted.

        The write is not read back.  It happens on every volume change, and a
        verification round trip would cost more than the write itself; a
        failed write is caught by the return code and re-resolves the node.
        """
        node = self.find_node()
        if node is None:
            return False
        controls = self.helper.loudness_controls(volume_db, enabled)
        if not self.helper.write_controls(node, controls):
            self.node = None
            return False
        self.applied_db = volume_db if enabled else None
        return True

    def wanted(self):
        """Whether the profile currently asks for compensation.

        Read rather than assumed, so that switching the compensation off does
        not race with an event already in flight here and turn it back on.
        The profile is only re-read when it has actually changed.
        """
        try:
            stamp = self.helper.PROFILE.stat().st_mtime
        except OSError:
            return False
        if stamp != self.profile_stamp:
            profile = self.helper.load_profile(self.helper.PROFILE)
            self.enabled = (profile or {}).get("loudness_compensation") == "on"
            self.profile_stamp = stamp
        return self.enabled

    def follow_volume(self):
        """Apply the current volume if it has moved enough to matter."""
        if not self.wanted():
            return self.apply(0.0, enabled=False) if self.applied_db is not None else True
        volume = self.helper.sink_volume_db(self.helper.VIRTUAL_SINK)
        if self.applied_db is not None and abs(volume - self.applied_db) < VOLUME_EPSILON_DB:
            return True
        return self.apply(volume)

    def run(self):
        if not self.wanted():
            # Started with the compensation switched off: the unit stays
            # registered between switches, so this happens at every login that
            # is not using it.  There is nothing to follow, so do not hold a
            # subscription open for it.
            return
        while self.running:
            if not self.follow_volume():
                time.sleep(RETRY_SECONDS)
                continue
            if not self.watch():
                time.sleep(RETRY_SECONDS)

    def watch(self):
        """Follow every sink change for as long as the subscription lives."""
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
                if "on sink" in line and not self.follow_volume():
                    return False
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
        tracker.run()
    finally:
        # Whatever went wrong, do not leave a quiet-level contour running.
        tracker.apply(0.0, enabled=False)


if __name__ == "__main__":
    main()
