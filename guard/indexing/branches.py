"""Indexes of base branches other than the default branch: the `index_branches` setting.

A pull request to a release branch, for example `version-15-hotfix`, gets the callers and callees of
that branch only when Guard indexes it. The setting comes from the dashboard and from the root config
at the head of the default branch, as all config does: a pull request cannot add a branch for itself.
Nested configs do not count, because the decision is for the whole repository.
"""

import frappe

from guard.config.globs import match_filter
from guard.config.resolver import ConfigResolver
from guard.git.repository import default_branch, default_head, fetch_default, store_for
from guard.git.store import GitError
from guard.indexing.indexer import index_lock
from guard.indexing.vectors import VectorStore
from guard.utils import db

# Each branch is a full copy of the graph rows, the chunks and the vectors of the repository.
MAX_BRANCH_INDEXES = 5
SETTING = "index_branches"


def patterns(repository: str, fetch: bool = False) -> list[str] | None:
	"""The `index_branches` patterns of a repository. None when Guard cannot read the config now.

	Read the default branch in the mirror. Fetch it first only with `fetch`, or when the mirror does
	not have it: the setting changes seldom, and `catch_up` fetches and reads it again every hour.
	"""
	try:
		store = store_for(repository)
		if fetch:
			fetch_default(repository, store)
		sha = default_head(repository, store)
		resolver = ConfigResolver.from_store(repository, store, sha)
	except GitError as error:
		frappe.logger("guard").warning(f"{repository}: cannot read {SETTING}: {error}")
		return None
	return list(resolver.resolve("").settings.get(SETTING) or [])


def listed(repository: str, branch: str, branch_patterns: list[str] | None) -> bool:
	"""The branch is not the default branch, and the setting lists it."""
	return is_other_branch(repository, branch) and match_filter(branch, branch_patterns)


def is_other_branch(repository: str, branch: str) -> bool:
	"""A branch, not a tag, and not the default branch."""
	return bool(branch) and not branch.startswith("refs/") and branch != default_branch(repository)


def branch_indexes(repository: str) -> dict[str, str]:
	"""The Repository Index of each branch other than the default branch: {branch: index}."""
	row = db.values("Code Repository", repository, ("default_branch", "default_index")) or {}
	rows = frappe.get_all(
		"Repository Index", filters={"repository": repository}, fields=["name", "branch"], order_by="creation"
	)
	return {
		str(r.branch): str(r.name)
		for r in rows
		if r.branch != row.get("default_branch") and r.name != row.get("default_index")
	}


def index_of(repository: str, branch: str, branch_patterns: list[str] | None) -> str | None:
	"""The index of a listed branch, created when missing. None when the setting does not list the
	branch, or the repository has MAX_BRANCH_INDEXES branch indexes already."""
	if not listed(repository, branch, branch_patterns):
		return None
	indexes = branch_indexes(repository)
	if branch in indexes:
		return indexes[branch]
	if len(indexes) >= MAX_BRANCH_INDEXES:
		frappe.logger("guard").warning(
			f"{repository}: not indexing branch {branch}: the repository has {len(indexes)} branch "
			f"indexes, and {MAX_BRANCH_INDEXES} is the limit"
		)
		return None
	from guard.indexing.jobs import index_for

	return index_for(repository, branch)


def on_push(repository: str, branch: str) -> bool:
	"""Queue an index of a pushed branch that the setting lists. True when it queued one."""
	# Read the config only for a branch that can be listed.
	if not is_other_branch(repository, branch):
		return False
	if not index_of(repository, branch, patterns(repository)):
		return False
	from guard.indexing.jobs import enqueue_index

	enqueue_index(repository, branch)
	return True


def catch_up() -> None:
	"""Every hour: drop the branch indexes that the setting no longer lists, and the indexes of
	branches that the remote no longer has. Index again each other one whose head moved past it.

	The index of the default branch is never dropped here.
	"""
	repositories = db.rows(
		"""SELECT DISTINCT i.repository FROM `tabRepository Index` i
		JOIN `tabCode Repository` r ON r.name = i.repository
		WHERE i.branch != IFNULL(r.default_branch, '') AND i.name != IFNULL(r.default_index, '')"""
	)
	for (repository,) in repositories:
		indexes = branch_indexes(repository)
		if not indexes:
			continue
		try:
			_catch_up(repository, indexes)
		except GitError as error:
			frappe.log_error(str(error), f"Guard branch index catch-up: {repository}")
		db.commit()


def _catch_up(repository: str, indexes: dict[str, str]) -> None:
	branch_patterns = patterns(repository, fetch=True)
	if branch_patterns is None:
		return  # Not on a guess: a read that failed must not drop an index.
	store = store_for(repository)
	remote = store.remote_branches()
	for branch, index in indexes.items():
		if branch not in remote or not listed(repository, branch, branch_patterns):
			drop_index(index)
			continue
		row = db.values("Repository Index", index, ("status", "sha")) or {}
		# As on the default branch: a run in progress or a failed run is not repeated here.
		if row.get("status") != "completed":
			continue
		store.fetch_branches(branch)
		if store.rev_parse(f"refs/heads/{branch}") != row.get("sha"):
			from guard.indexing.jobs import enqueue_index

			enqueue_index(repository, branch)


def drop_index(index: str) -> bool:
	"""Delete the rows and the vectors of an index, and the Repository Index. Never the default
	index of a repository. False when the index is in use or is a default index."""
	repository = str(db.value("Repository Index", index, "repository") or "")
	if not repository or frappe.db.exists("Code Repository", {"default_index": index}):
		return False
	if db.value("Repository Index", index, "branch") == default_branch(repository):
		return False
	with index_lock(index) as acquired:
		if not acquired:
			return False  # A run writes it now. The next catch-up drops it.
		chunks = [r[0] for r in db.rows("SELECT name FROM `tabCode Chunk` WHERE `index` = %s", (index,))]
		VectorStore().delete_refs("chunk", chunks)
		for doctype in ("Code Entity", "Code Edge", "Code Chunk"):
			frappe.db.delete(doctype, {"index": index})
		frappe.delete_doc("Repository Index", index, ignore_permissions=True, force=True)
		db.commit()
	frappe.logger("guard").info(f"{repository}: dropped index {index}")
	return True
