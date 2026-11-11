"""One process per checkout, with an OS-owned lock released on exit."""
from contextlib import contextmanager
import errno
import os


@contextmanager
def exclusive_lock(path):
    # Do not truncate or unlink this file: other processes may have it open.
    with open(path, 'a+b') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise SystemExit('Tether Companion or the enabler is already running. Close it first.') from exc
            raise
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)
