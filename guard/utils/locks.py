"""Locks that the kernel drops when the process that holds them dies.

A deploy or the OOM killer can stop a worker at any line. A lock in Redis then stays held until
its timeout, and every job that needs it waits or fails. These locks are files, so they only
work between the processes of one server, which is how a bench runs its workers.
"""

import fcntl
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

POLL = 0.5


@contextmanager
def file_lock(path: Path, wait: float = 0) -> Iterator[bool]:
	"""Hold an exclusive lock on `path` while the block runs. Yield False when another process
	still holds it after `wait` seconds."""
	path.parent.mkdir(parents=True, exist_ok=True)
	fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
	try:
		deadline = time.monotonic() + wait
		while True:
			try:
				fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
				break
			except BlockingIOError:
				if time.monotonic() >= deadline:
					yield False
					return
				time.sleep(POLL)
		yield True
	finally:
		# Closing the file drops the lock.
		os.close(fd)
