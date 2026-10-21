"""Runtime validation: run commands against the pull request head in a container.

The work tree is a copy of the head commit. The container has no network unless the
setting allows it, and CPU, memory and time limits. Nothing from the site is mounted.
"""

import shutil
import subprocess
import tarfile
import time
from io import BytesIO
from pathlib import Path
from types import TracebackType

import frappe
from frappe.utils import cint

from guard import settings as guard_settings
from guard.review.run import ReviewRun
from guard.utils import db
from guard.utils.redact import redact

OUTPUT_LIMIT = 20_000
MEMORY = "2g"
CPUS = "2"


class SandboxError(Exception):
	pass


def wanted(run: ReviewRun) -> bool:
	"""Settings allow it, and the pull request is not from a fork, unless an admin asked."""
	doc = guard_settings.settings()
	if not doc.sandbox_enabled or doc.sandbox_when == "Never":
		return False
	if doc.sandbox_when == "On request" and not _requested(run.instructions):
		return False
	if run.pr is not None and _is_fork(run):
		user = run.review.get("triggered_by_user") if run.review is not None else None
		return bool(user and "Guard Admin" in frappe.get_roles(user))
	return runtime_path(doc.sandbox_runtime or "podman") is not None


class Sandbox:
	def __init__(self, run: ReviewRun):
		self.run = run
		doc = guard_settings.settings()
		self.runtime = runtime_path(doc.sandbox_runtime or "podman") or "podman"
		self.image = doc.sandbox_image or "docker.io/library/python:3.14-slim"
		self.setup = doc.sandbox_setup_command or ""
		self.timeout = cint(doc.sandbox_timeout) or 600
		self.network = bool(doc.sandbox_network)
		self.root = Path(
			frappe.get_site_path(
				"private",
				"guard",
				"sandbox",
				str(run.review.name if run.review is not None else frappe.generate_hash(length=8)),
			)
		)
		self.deadline = time.time() + self.timeout
		self.ready = False

	def __enter__(self) -> "Sandbox":
		return self

	def __exit__(
		self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None
	) -> None:
		self.cleanup()

	def cleanup(self) -> None:
		"""Delete the work tree. A leftover folder is harmless, so errors are logged only."""
		try:
			shutil.rmtree(self.root)
		except FileNotFoundError:
			pass
		except OSError as error:
			frappe.log_error(str(error), "Guard sandbox cleanup")

	def prepare(self) -> None:
		if self.ready:
			return
		self.root.mkdir(parents=True, exist_ok=True)
		from guard.git.repository import store_for

		if not self.run.repository:
			raise SandboxError("CLI reviews have no repository copy to run")
		archive = subprocess.run(
			["git", "-C", str(store_for(self.run.repository).path), "archive", self.run.head_sha],
			capture_output=True,
			timeout=300,
			check=False,
		)
		if archive.returncode:
			raise SandboxError("Could not copy the head commit")
		with tarfile.open(fileobj=BytesIO(archive.stdout)) as tar:
			tar.extractall(self.root, filter="data")
		self.ready = True
		if self.setup:
			result = self.exec(self.setup, record=False)
			if result["exit_code"]:
				raise SandboxError(f"Setup failed:\n{result['output'][-4000:]}")

	def write(self, path: str, content: str) -> str:
		self.prepare()
		target = (self.root / path).resolve()
		if not str(target).startswith(str(self.root.resolve())):
			raise SandboxError("The path leaves the work tree")
		target.parent.mkdir(parents=True, exist_ok=True)
		target.write_text(content)
		return f"wrote {path}"

	def exec(self, command: str, record: bool = True, finding: str | None = None) -> dict:
		self.prepare()
		remaining = int(self.deadline - time.time())
		if remaining <= 5:
			raise SandboxError("The sandbox time limit is used up")
		args = [
			self.runtime,
			"run",
			"--rm",
			"--memory",
			MEMORY,
			"--cpus",
			CPUS,
			"--pids-limit",
			"512",
			"-v",
			f"{self.root}:/work:Z",
			"-w",
			"/work",
		]
		args += [] if self.network else ["--network", "none"]
		args += ["--security-opt", "no-new-privileges", "--cap-drop", "ALL", self.image, "sh", "-c", command]
		started = time.time()
		try:
			proc = subprocess.run(args, capture_output=True, timeout=remaining, check=False)
			code, status = proc.returncode, "Passed" if proc.returncode == 0 else "Failed"
			output = (proc.stdout + proc.stderr).decode("utf-8", "replace")
		except subprocess.TimeoutExpired as error:
			code, status = -1, "Timed Out"
			output = ((error.stdout or b"") + (error.stderr or b"")).decode("utf-8", "replace")
		output = redact(output)[-OUTPUT_LIMIT:]
		if record and self.run.review is not None:
			frappe.get_doc(
				{
					"doctype": "Sandbox Run",
					"code_review": self.run.review.name,
					"finding": finding,
					"command": command[:1000],
					"exit_code": code,
					"status": status,
					"duration": round(time.time() - started, 2),
					"output": output,
				}
			).insert(ignore_permissions=True)
		return {"exit_code": code, "status": status, "output": output}


def runtime_path(name: str) -> str | None:
	return shutil.which(name)


def _requested(instructions: str) -> bool:
	return any(
		word in (instructions or "").lower()
		for word in ("run the tests", "run tests", "sandbox", "execute", "runtime")
	)


def _is_fork(run: ReviewRun) -> bool:
	head = str(run.pr.get("head_clone_url") or "") if run.pr is not None else ""
	clone = str(db.value("Code Repository", run.repository, "clone_url") or "") if run.repository else ""
	return bool(head) and head.rstrip("/").removesuffix(".git") != clone.rstrip("/").removesuffix(".git")
