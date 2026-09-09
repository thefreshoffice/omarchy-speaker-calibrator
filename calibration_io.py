"""Reading and writing files that something else could have replaced.

Every path here is one another process running as this user can reach: state
under ``~/.local/share``, generated PipeWire and systemd fragments, a
microphone calibration file the user picked, plugin descriptions under
``/usr/lib/lv2``.  A symlink planted on any of those names turns an ordinary
read into a read of something else and an ordinary write into a truncation of
it, and a FIFO turns a read into a hang that would take the whole shell down
with it, because every widget shares one process.

So: open once, check the descriptor, use that same descriptor.  Never test a
name and then open it, which is a different file by the time it is opened.
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

# Enough for any state this plugin writes; a measurement is summarised, never
# stored raw.  Overflow is refused rather than truncated, because half a JSON
# document parses into something arbitrary.
MAX_STATE_BYTES = 1 << 20
MAX_CALIBRATION_BYTES = 1 << 20
MAX_DESCRIPTION_BYTES = 1 << 20


class UnsafeFile(OSError):
    """The name resolved to something that must not be read or written."""


def read_bounded(path, limit=MAX_STATE_BYTES, *, missing_ok=True, allow_root=False):
    """Bytes of a regular file this user owns, or None when it is absent.

    ``O_NOFOLLOW`` refuses a symlink at the final component and ``O_NONBLOCK``
    keeps a planted FIFO from blocking the open forever; the type is then
    checked on the descriptor that was actually opened, not on the name.

    ``allow_root`` also accepts a file owned by root, for the ones the system
    installed rather than this plugin: a package's own description under
    ``/usr/lib`` is more trustworthy than anything here, not less.  What is
    refused either way is a file belonging to some other unprivileged user.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise
    except OSError as error:
        raise UnsafeFile(f"refusing to read {path}: {error}") from error
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise UnsafeFile(f"refusing to read {path}: not a regular file")
        permitted = {os.geteuid()} | ({0} if allow_root else set())
        if info.st_uid not in permitted:
            raise UnsafeFile(f"refusing to read {path}: owned by another user")
        if info.st_size > limit:
            raise UnsafeFile(f"refusing to read {path}: larger than {limit} bytes")
        os.set_blocking(fd, True)
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(fd, min(1 << 16, limit + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            raise UnsafeFile(f"refusing to read {path}: grew past {limit} bytes")
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_text_bounded(path, limit=MAX_STATE_BYTES, *, errors="strict",
                      missing_ok=True, allow_root=False):
    raw = read_bounded(path, limit, missing_ok=missing_ok, allow_root=allow_root)
    return None if raw is None else raw.decode("utf-8", errors)


# A state directory holds a handful of files; this only bounds the pathological
# case, where the walk would otherwise be unbounded work on every write.
MAX_REPAIRED_ENTRIES = 4096


def secure_directory(path, *, repair_contents=False):
    """Make sure a directory this plugin owns exists, is one, and is private.

    With ``repair_contents`` the entries are tightened as well, because the
    mode of a directory says nothing about what is inside it: one that is 0700
    today can still hold world-readable files written when it was not, and
    those would otherwise never be repaired.
    """
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISDIR(info.st_mode):
            raise UnsafeFile(f"refusing to use {directory}: not a directory")
        if info.st_uid != os.geteuid():
            raise UnsafeFile(f"refusing to use {directory}: owned by another user")
        if info.st_mode & 0o077:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    if repair_contents:
        _tighten(directory)
    return directory


def _tighten(directory, budget=MAX_REPAIRED_ENTRIES):
    """Give everything under a plugin-owned directory owner-only permissions.

    Symlinks are stepped over rather than followed or deleted: nothing here
    opens a file by name anyway, so one cannot redirect a read or a write, and
    removing a file this plugin did not create is not this function's business.
    """
    pending = [directory]
    while pending and budget > 0:
        current = pending.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if budget <= 0:
                break
            budget -= 1
            try:
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if entry.stat(follow_symlinks=False).st_mode & 0o077:
                        os.chmod(entry.path, 0o700)
                    pending.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    if entry.stat(follow_symlinks=False).st_mode & 0o077:
                        os.chmod(entry.path, 0o600)
            except OSError:
                continue


def write_atomic(path, text, *, mode=0o600):
    """Publish a file without ever writing through whatever is at its name.

    The bytes go to a fresh unpredictable name in the same directory, created
    exclusively so it cannot be an existing symlink, and ``rename`` then
    replaces the destination.  A rename replaces a symlink sitting there
    instead of following it, which a plain open would not.
    """
    destination = Path(path)
    secure_directory(destination.parent)
    payload = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    temporary = destination.parent / f".{destination.name}.{secrets.token_hex(8)}.tmp"
    fd = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        mode,
    )
    try:
        os.fchmod(fd, mode)
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    else:
        os.close(fd)
    try:
        os.replace(temporary, destination)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(parent)
    finally:
        os.close(parent)
    return destination
