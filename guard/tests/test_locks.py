import signal
import subprocess
import sys
import tempfile
from pathlib import Path

from frappe.tests import UnitTestCase

from guard.utils.locks import file_lock

# Take the lock, say so, and hold it until killed.
HOLD = """
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_CREAT | os.O_RDWR, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX)
print("held", flush=True)
time.sleep(60)
"""


class TestFileLock(UnitTestCase):
	def test_a_killed_holder_lets_go(self):
		# A deploy kills workers with the lock held. The next job must not wait for a timeout.
		path = Path(tempfile.mkdtemp()) / "index.lock"
		holder = subprocess.Popen([sys.executable, "-c", HOLD, str(path)], stdout=subprocess.PIPE, text=True)
		self.addCleanup(holder.kill)
		self.assertEqual(holder.stdout.readline().strip() if holder.stdout else "", "held")
		with file_lock(path) as acquired:
			self.assertFalse(acquired)
		holder.send_signal(signal.SIGKILL)
		holder.wait()
		with file_lock(path) as acquired:
			self.assertTrue(acquired)
