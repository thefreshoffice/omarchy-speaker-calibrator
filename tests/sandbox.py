"""Keep every test away from this machine's real calibrator.

Each test module imports this before it loads the helper.  The helper works
out its configuration and data folders from XDG_CONFIG_HOME and XDG_DATA_HOME
when it is loaded, so pointing both at a throwaway folder keeps its files out
of the real ones, whatever a test forgets to redirect.

Files are only half of it.  A test that forgets to fake one command would
otherwise reach the running audio system: a filter control written to the
real tuning node changes what the speakers play.  Those commands are refused
here, so such a test fails instead.  A test that fakes them (mock.patch of
``run``, ``pactl_json`` or ``subprocess.run``) is unaffected.
"""

import atexit
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(tempfile.mkdtemp(prefix="speaker-calibrator-tests-"))
atexit.register(shutil.rmtree, ROOT, True)
os.environ["XDG_CONFIG_HOME"] = str(ROOT / "config")
os.environ["XDG_DATA_HOME"] = str(ROOT / "data")

# Everything the helper starts that talks to the session's audio, its
# services, the desktop or GitHub.
LIVE_COMMANDS = frozenset((
    "systemctl", "pactl", "pw-cli", "pw-dump", "pw-play", "pw-record", "pw-metadata",
    "wpctl", "amixer", "omarchy", "omarchy-audio-tuning", "gh", "notify-send", "xdg-open",
))


class LiveSystemCall(AssertionError):
    """A test reached the real audio system."""


def _refuse_live(args):
    argv = args if isinstance(args, (list, tuple)) else str(args).split()
    name = os.path.basename(str(argv[0])) if argv else ""
    if name in LIVE_COMMANDS:
        raise LiveSystemCall(f"a test tried to run {list(argv)!r} on this machine; fake it instead")


class _GuardedPopen(subprocess.Popen):
    def __init__(self, args, *rest, **options):
        _refuse_live(args)
        super().__init__(args, *rest, **options)


if not getattr(subprocess, "_speaker_calibrator_sandbox", False):
    # subprocess.run starts its process through Popen, so this covers both.
    subprocess.Popen = _GuardedPopen
    subprocess._speaker_calibrator_sandbox = True
