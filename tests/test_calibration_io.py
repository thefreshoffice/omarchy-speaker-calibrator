"""What the file helpers must refuse.

Every case here is something another process running as this user can set up
at a path this plugin uses, so each one is written the way an attacker would
and then checked for the refusal rather than for the happy path.
"""

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calibration_io import (  # noqa: E402
    UnsafeFile,
    read_bounded,
    read_text_bounded,
    secure_directory,
    write_atomic,
)


class SafeReadTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_it_reads_an_ordinary_file(self):
        target = self.root / "state.json"
        target.write_text('{"a": 1}')
        self.assertEqual(read_text_bounded(target), '{"a": 1}')

    def test_a_missing_file_is_absent_not_an_error(self):
        self.assertIsNone(read_bounded(self.root / "gone"))

    def test_it_refuses_a_symlink(self):
        # The classic: a link planted on a name the plugin will read, pointing
        # at something it should never have opened.
        secret = self.root / "secret"
        secret.write_text("private")
        link = self.root / "state.json"
        link.symlink_to(secret)
        with self.assertRaises(UnsafeFile):
            read_bounded(link)

    def test_it_refuses_something_that_is_not_a_regular_file(self):
        # A FIFO here would block the open forever without O_NONBLOCK, and the
        # panel shares one process with the whole shell.
        fifo = self.root / "state.json"
        os.mkfifo(fifo)
        with self.assertRaises(UnsafeFile):
            read_bounded(fifo)

    def test_it_refuses_a_file_past_the_limit(self):
        target = self.root / "big.json"
        target.write_text("x" * 4096)
        self.assertIsNotNone(read_bounded(target, 8192))
        with self.assertRaises(UnsafeFile):
            read_bounded(target, 1024)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_it_creates_the_file_owner_only(self):
        target = write_atomic(self.root / "state.json", "{}")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_it_replaces_a_symlink_instead_of_writing_through_it(self):
        # Writing through the link would truncate the victim; a rename puts a
        # real file where the link was and leaves the victim alone.
        victim = self.root / "victim"
        victim.write_text("must survive")
        link = self.root / "state.json"
        link.symlink_to(victim)
        write_atomic(link, "replaced")
        self.assertEqual(victim.read_text(), "must survive")
        self.assertFalse(link.is_symlink())
        self.assertEqual(link.read_text(), "replaced")

    def test_it_leaves_no_temporary_behind(self):
        write_atomic(self.root / "state.json", "{}")
        self.assertEqual([p.name for p in self.root.iterdir()], ["state.json"])

    def test_a_failed_write_leaves_no_temporary_behind(self):
        class Unencodable:
            def __str__(self):
                raise ValueError("boom")

        with self.assertRaises(Exception):
            write_atomic(self.root / "state.json", Unencodable())
        self.assertEqual(list(self.root.iterdir()), [])


class DirectoryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_it_makes_the_directory_private(self):
        directory = self.root / "state"
        directory.mkdir(mode=0o755)
        secure_directory(directory)
        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_it_repairs_files_left_readable_by_an_earlier_version(self):
        # A directory that is 0700 today can still hold 0644 files from before.
        directory = self.root / "state"
        directory.mkdir(mode=0o700)
        stale = directory / "active-profile.json"
        stale.write_text("{}")
        os.chmod(stale, 0o644)
        nested = directory / "backups"
        nested.mkdir(mode=0o755)
        buried = nested / "old.json"
        buried.write_text("{}")
        os.chmod(buried, 0o644)

        secure_directory(directory, repair_contents=True)

        self.assertEqual(stat.S_IMODE(stale.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(nested.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(buried.stat().st_mode), 0o600)

    def test_the_repair_steps_over_symlinks_without_following_them(self):
        directory = self.root / "state"
        directory.mkdir(mode=0o700)
        outside = self.root / "outside"
        outside.write_text("not ours")
        os.chmod(outside, 0o644)
        (directory / "link").symlink_to(outside)

        secure_directory(directory, repair_contents=True)

        self.assertEqual(stat.S_IMODE(outside.stat().st_mode), 0o644)
        self.assertTrue((directory / "link").is_symlink())


if __name__ == "__main__":
    unittest.main()
