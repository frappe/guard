"""Test helpers: temporary git repositories and Guard records."""

import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

import frappe

GIT_ENV = {
	"GIT_AUTHOR_NAME": "Test",
	"GIT_AUTHOR_EMAIL": "test@example.com",
	"GIT_COMMITTER_NAME": "Test",
	"GIT_COMMITTER_EMAIL": "test@example.com",
	"PATH": "/usr/bin:/bin:/usr/local/bin",
}


class TempRepo:
	"""A git repository in a temporary folder."""

	def __init__(self, files: Mapping[str, str | None], branch: str = "main"):
		self.dir = tempfile.TemporaryDirectory(prefix="guard-test-")
		self.path = Path(self.dir.name)
		self.branch = branch
		self.git("init", "-q", "-b", branch)
		self.commit(files, "initial")

	def git(self, *args: str) -> str:
		result = subprocess.run(
			["git", *args], cwd=self.path, capture_output=True, text=True, env=GIT_ENV, check=True
		)
		return result.stdout.strip()

	def commit(self, files: Mapping[str, str | None], message: str) -> str:
		for path, content in files.items():
			target = self.path / path
			if content is None:
				target.unlink(missing_ok=True)
				continue
			target.parent.mkdir(parents=True, exist_ok=True)
			target.write_text(content)
		self.git("add", "-A")
		self.git("commit", "-q", "-m", message)
		return self.head()

	def head(self) -> str:
		return self.git("rev-parse", "HEAD")

	def branch_off(self, name: str) -> None:
		self.git("checkout", "-q", "-b", name)

	def cleanup(self) -> None:
		self.dir.cleanup()


def make_repository(repo: TempRepo, full_name: str = "acme/app", provider: str = "Test Provider") -> str:
	if not frappe.db.exists("Code Provider", provider):
		frappe.get_doc({"doctype": "Code Provider", "title": provider, "provider_type": "GitHub"}).insert(
			ignore_permissions=True
		)
	doc = frappe.get_doc(
		{
			"doctype": "Code Repository",
			"full_name": full_name,
			"code_provider": provider,
			"clone_url": str(repo.path),
			"default_branch": repo.branch,
			"reviews_enabled": 1,
		}
	).insert(ignore_permissions=True)
	return str(doc.name)
