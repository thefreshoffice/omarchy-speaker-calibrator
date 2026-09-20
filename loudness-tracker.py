#!/usr/bin/python3
"""Tell the loudness compensator what level the speakers are playing at.

The ear loses bass as the level drops, so quiet music sounds thin.  The
compensator in the filter graph can undo that, but only if it is told the
listening level, and only the output device knows it.  This watches the
volume and passes it on.

The volume it watches is the physical device's, not the filter's.  The volume
keys resolve through a DSP sink to the device behind it so that they move real
loudness and the processing keeps seeing full-scale input, which means the
filter's own volume sits at full however quiet the room is.

It holds one subscription open for the life of the service rather than
resubscribing after every change: restarting it each time leaves a window in
which a volume move is missed, and a missed move leaves the wrong contour
applied until the next one.

It writes the compensator's controls and one gain downstream of it, the
limiter's input gain, which pays back the attenuation the contour comes with.
That gain is never invented here: it is the calibrated value from the profile
multiplied by the make-up, so the worst a failure can do is leave the wrong
loudness, never the wrong correction.  On the way out it switches the
compensation off and puts that gain back, which is the safe state: leaving a
low-volume contour applied while the volume is high would be heard as far too
much bass, and leaving its make-up behind would be heard as too loud.
"""

import importlib.util
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

# Loading the helper compiles it, and the bytecode must not land in the plugin
# directory: Omarchy's shell reloads the plugin on any change there. Same rule,
# same place as in the helper itself.
sys.dont_write_bytecode = True

HELPER = Path(__file__).resolve().parent / "speaker-calibrate.py"
# Below this the change is inaudible and not worth a round of control writes.
VOLUME_EPSILON_DB = 0.4
RETRY_SECONDS = 2.0
# Between the writes of one ramp; a write itself takes about fifteen.
RAMP_INTERVAL_SECONDS = 0.02
# A held volume key fires a change every forty milliseconds or so. Applying
# each one is heard as a crackle, so a burst is followed to its end and the
# level it ended at is applied once; the limit keeps a slider drag responsive.
SETTLE_SECONDS = 0.12
SETTLE_LIMIT_SECONDS = 0.6


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
        self.input_gain = 1.0
        self.sink = None
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
        target = self.helper.loudness_controls(volume_db, enabled, self.input_gain)
        if enabled and self.applied_db is not None:
            # Known start, so the change can be walked rather than jumped.
            start = self.helper.loudness_controls(self.applied_db, True, self.input_gain)
            writes = self.helper.loudness_ramp(start, target)
        else:
            # The first write after a start also carries the limiter's timing:
            # a graph loaded from a file written by an older version still has
            # the limiter on the defaults that were audible on bass.
            timing = getattr(self.helper, "limiter_timing_controls", dict)()
            writes = [dict(target, **timing)]
        for index, controls in enumerate(writes):
            if index:
                time.sleep(RAMP_INTERVAL_SECONDS)
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
            profile = self.helper.load_profile(self.helper.PROFILE) or {}
            self.enabled = profile.get("loudness_compensation") == "on"
            # The make-up rides on the limiter's gain, so the calibrated value
            # it builds on has to come from the same profile.
            self.input_gain = float(
                (profile.get("fit") or {}).get("input_gain_linear", 1.0))
            # The device behind the filter, not the filter: the volume keys
            # resolve through it, so its own volume never moves.
            self.sink = self.helper.listening_sink(profile)
            self.profile_stamp = stamp
        return self.enabled

    def follow_volume(self):
        """Apply the current volume if it has moved enough to matter."""
        if not self.wanted():
            return self.apply(0.0, enabled=False) if self.applied_db is not None else True
        volume = self.helper.sink_volume_db(self.sink or self.helper.VIRTUAL_SINK)
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
            # Unbuffered, so that select() below sees exactly what is unread.
            events = subprocess.Popen(
                ["pactl", "subscribe"], stdout=subprocess.PIPE, bufsize=0
            )
        except OSError:
            return False
        try:
            out = events.stdout
            while True:
                line = out.readline()
                if not line:
                    return False
                if not self.running:
                    return True
                if b"on sink" not in line:
                    continue
                # Let a burst of changes end before acting on it.
                ended = False
                deadline = time.monotonic() + SETTLE_LIMIT_SECONDS
                while time.monotonic() < deadline:
                    ready, _, _ = select.select([out], [], [], SETTLE_SECONDS)
                    if not ready:
                        break
                    if not out.readline():
                        ended = True
                        break
                if not self.follow_volume():
                    return False
                if ended:
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
        # Only when something was actually applied, though: the reset writes
        # the calibrated gain back, and that value is only known once the
        # profile has been read.
        if tracker.applied_db is not None:
            tracker.apply(0.0, enabled=False)


if __name__ == "__main__":
    main()
