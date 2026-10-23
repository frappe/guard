"""Index large real repositories and fail on any error. The nightly CI job runs it.

Parser edge cases in big codebases have failed whole indexes in production. This finds them
before a user does:

    bench --site test_site execute guard.indexing.check.index_repositories
"""

import time
from dataclasses import dataclass

import frappe

from guard.indexing.indexer import Indexer
from guard.indexing.jobs import index_for
from guard.utils import db

DEFAULT = "frappe/erpnext@develop,frappe/frappe@develop,frappe/suite@develop"
PROVIDER = "Index Check"


class IndexCheckFailed(Exception):
	pass


@dataclass
class Result:
	full_name: str
	seconds: float
	files: int = 0
	entities: int = 0
	error: str = ""


def index_repositories(repositories: str = DEFAULT) -> None:
	"""Index each `owner/name@branch` from GitHub, from scratch. Raise if one fails.

	Returns nothing: `bench execute` prints the return value as JSON.
	"""
	targets = []
	for spec in repositories.split(","):
		full_name, _, branch = spec.strip().partition("@")
		targets.append((full_name, f"https://github.com/{full_name}.git", branch or "develop"))
	check(targets)


def check(targets: list[tuple[str, str, str]]) -> list[Result]:
	"""Index each (full_name, clone_url, branch) and print a table. Raise if one fails."""
	results = [_index(*target) for target in targets]
	for r in results:
		state = f"FAILED {r.error}" if r.error else "ok"
		print(f"{r.full_name:24} {r.files:6} files {r.entities:7} entities {r.seconds:7.1f}s  {state}")
	failed = [r for r in results if r.error]
	if failed:
		raise IndexCheckFailed(", ".join(f"{r.full_name}: {r.error}" for r in failed))
	return results


def _index(full_name: str, clone_url: str, branch: str) -> Result:
	started = time.monotonic()
	repository = _repository(full_name, clone_url, branch)
	index = index_for(repository, branch)
	try:
		Indexer(index, full=True).run()
	except Exception as error:
		return Result(full_name, time.monotonic() - started, error=f"{type(error).__name__}: {error}"[:300])
	row = db.values("Repository Index", index, ("num_files", "entity_count")) or {}
	return Result(
		full_name,
		time.monotonic() - started,
		int(row.get("num_files") or 0),
		int(row.get("entity_count") or 0),
	)


def _repository(full_name: str, clone_url: str, branch: str) -> str:
	if not frappe.db.exists("Code Provider", PROVIDER):
		# No token: public repositories clone without one.
		frappe.get_doc({"doctype": "Code Provider", "title": PROVIDER, "provider_type": "GitHub"}).insert(
			ignore_permissions=True
		)
	name = db.value("Code Repository", {"full_name": full_name, "code_provider": PROVIDER}, "name")
	if name:
		return str(name)
	doc = frappe.get_doc(
		{
			"doctype": "Code Repository",
			"full_name": full_name,
			"code_provider": PROVIDER,
			"clone_url": clone_url,
			"default_branch": branch,
		}
	).insert(ignore_permissions=True)
	db.commit()
	return str(doc.name)
