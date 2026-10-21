import os
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from guard.git.repository import store_for
from guard.review import sandbox
from guard.review.run import ReviewRun
from guard.tests.utils import TempRepo, make_repository

# Stands in for podman: honours `-v <dir>:/work` and runs the trailing `sh -c <command>` there.
SHIM = """#!/bin/sh
dir=""
while [ $# -gt 0 ]; do
	case "$1" in
		-v) dir="${2%%:*}"; shift 2 ;;
		sh) shift; shift; cd "$dir" && exec sh -c "$1" ;;
		*) shift ;;
	esac
done
"""


class TestSandbox(IntegrationTestCase):
	def setUp(self):
		self.repo = TempRepo({"calc.py": "def add(a, b):\n    return a - b\n"})
		self.repository = make_repository(self.repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		store_for(self.repository).fetch()
		self.shim = Path(tempfile.mkdtemp()) / "fake-podman"
		self.shim.write_text(SHIM)
		self.shim.chmod(self.shim.stat().st_mode | stat.S_IEXEC)
		settings = frappe.get_single("Guard Settings")
		settings.update(
			{
				"sandbox_enabled": 1,
				"sandbox_when": "Always",
				"sandbox_runtime": "podman",
				"sandbox_timeout": 60,
			}
		)
		settings.save()
		self.patcher = patch("guard.review.sandbox.runtime_path", return_value=str(self.shim))
		self.patcher.start()

	def tearDown(self):
		self.patcher.stop()
		self.repo.cleanup()
		os.remove(self.shim)

	def run_for(self):
		review = frappe.get_doc(
			{"doctype": "Code Review", "repository": self.repository, "source": "api", "status": "Running"}
		).insert()
		return ReviewRun(
			review=review,
			repository=self.repository,
			full_name="acme/x",
			head=lambda p: None,
			base=lambda p: None,
			list_dir=lambda p: [],
			head_sha=self.repo.head(),
			base_sha=self.repo.head(),
			pr=SimpleNamespace(get=lambda key, default=None: None),
		)

	def test_runs_tests_against_the_head(self):
		run = self.run_for()
		self.assertTrue(sandbox.wanted(run))
		with sandbox.Sandbox(run) as box:
			box.write("test_calc.py", "from calc import add\nassert add(2, 2) == 4, 'add is wrong'\n")
			result = box.exec("python3 test_calc.py")
			root = box.root
		self.assertEqual(result["status"], "Failed")
		self.assertIn("add is wrong", result["output"])
		self.assertFalse(root.exists())
		self.assertEqual(
			frappe.db.count("Sandbox Run", {"code_review": run.review.name, "status": "Failed"}), 1
		)

	def test_paths_cannot_leave_the_work_tree(self):
		with sandbox.Sandbox(self.run_for()) as box, self.assertRaises(sandbox.SandboxError):
			box.write("../escape.txt", "x")

	def test_on_request_needs_words_in_instructions(self):
		frappe.db.set_single_value("Guard Settings", "sandbox_when", "On request")
		run = self.run_for()
		self.assertFalse(sandbox.wanted(run))
		run.instructions = "please run the tests"
		self.assertTrue(sandbox.wanted(run))
