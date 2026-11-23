"""Indexes of base branches other than the default branch: the `index_branches` setting.

A pull request to a release branch, for example `version-15-hotfix`, gets the callers and callees of
that branch only when Guard indexes it. The setting comes from the dashboard and from the root config
at the head of the default branch, as all config does: a pull request cannot add a branch for itself.
Nested configs do not count, because the decision is for the whole repository.

Guard marks the indexes that it makes for the setting with `auto`. It drops them when the setting no
longer lists the branch. An index that the query API made has no mark: Guard drops it only when the
remote no longer has the branch.

Branch names are compared without case, as MariaDB compares the `branch` column.
"""

from dataclasses import dataclass

import frappe
from frappe.utils import now_datetime

from guard.config.globs import match_filter
from guard.config.resolver import GUARD_DIR, SINGLE_FILE, ConfigResolver
from guard.git.repository import default_branch, default_head, fetch_default, store_for
from guard.git.store import GitError, GitStore
from guard.indexing.indexer import RUNNING, index_lock, recent_failure
from guard.indexing.vectors import VectorStore
from guard.utils import db
from guard.utils.redact import redact

# Each branch is a full copy of the graph rows, the chunks and the vectors of the repository.
MAX_BRANCH_INDEXES = 5
SETTING = "index_branches"
# Rows deleted in one transaction when an index is dropped. A large repository has a million edges:
# one DELETE of all of them holds locks and undo log for minutes.
DROP_BATCH = 5000
# The graph tables, edges first: they point at entities.
GRAPH_TABLES = ("tabCode Edge", "tabCode Entity")


@dataclass
class BranchIndex:
	name: str
	branch: str
	auto: bool
	status: str
	sha: str


def same_branch(a: str | None, b: str | None) -> bool:
	return (a or "").casefold() == (b or "").casefold()


def patterns(repository: str, fetch: bool = False) -> list[str] | None:
	"""The `index_branches` patterns of a repository. None when Guard cannot read the setting now.

	Read the default branch in the mirror. Fetch it first only with `fetch`, or when the mirror does
	not have it: the setting changes seldom, and `catch_up` fetches and reads it again every hour.

	None means "unknown": the caller drops nothing and queues nothing. An empty list would drop every
	index that Guard made, so a root config that Guard cannot read or parse gives None, not [].
	"""
	try:
		store = store_for(repository)
		if fetch:
			fetch_default(repository, store)
		sha = default_head(repository, store)
		paths = [entry.path for entry in store.ls_tree(sha) if _root_config(entry.path)]
	except GitError as error:
		return _unknown(repository, str(error))
	files: dict[str, str] = {}
	for path in paths:
		# `show` gives None on a git error or a timeout, also for a file that the tree has.
		text = store.show(sha, path)
		if text is None:
			return _unknown(repository, f"cannot read {path} at {sha[:12]}")
		files[path] = text
	resolver = ConfigResolver.from_files(repository, files)
	settings = resolver.resolve("").settings
	if resolver.errors:
		return _unknown(repository, "; ".join(resolver.errors))
	return list(settings.get(SETTING) or [])


def _root_config(path: str) -> bool:
	"""A config file of the root. Nested configs do not count for the whole repository."""
	return path.startswith(f"{GUARD_DIR}/") or path == SINGLE_FILE


def _unknown(repository: str, reason: str) -> None:
	frappe.logger("guard").warning(f"{repository}: cannot read {SETTING}: {redact(reason)[:500]}")
	return None


def listed(repository: str, branch: str, branch_patterns: list[str] | None) -> bool:
	"""The branch is not the default branch, and the setting lists it."""
	return is_other_branch(repository, branch) and match_filter(branch, branch_patterns)


def is_other_branch(repository: str, branch: str) -> bool:
	"""A branch, not a tag, and not the default branch."""
	return (
		bool(branch)
		and not branch.startswith("refs/")
		and not same_branch(branch, default_branch(repository))
	)


def branch_indexes(repository: str) -> list[BranchIndex]:
	"""The Repository Index of each branch other than the default branch, oldest first."""
	row = db.values("Code Repository", repository, ("default_branch", "default_index")) or {}
	rows = frappe.get_all(
		"Repository Index",
		filters={"repository": repository},
		fields=["name", "branch", "auto", "status", "sha"],
		order_by="creation, name",
	)
	return [
		BranchIndex(str(r.name), str(r.branch), bool(r.auto), str(r.status or ""), str(r.sha or ""))
		for r in rows
		if not same_branch(r.branch, row.get("default_branch")) and r.name != row.get("default_index")
	]


def kept(repository: str, indexes: list[BranchIndex], branch_patterns: list[str] | None) -> list[BranchIndex]:
	"""The marked indexes that the setting lists, up to the limit. The oldest win: a new branch never
	takes the place of a branch that has an index."""
	wanted = [i for i in indexes if i.auto and listed(repository, i.branch, branch_patterns)]
	return wanted[:MAX_BRANCH_INDEXES]


def index_of(
	repository: str, branch: str, branch_patterns: list[str] | None, create: bool = True
) -> str | None:
	"""The index of a listed branch. With `create`, make a missing one. None when the setting does not
	list the branch, or the repository has MAX_BRANCH_INDEXES marked indexes of listed branches."""
	if not listed(repository, branch, branch_patterns):
		return None
	indexes = branch_indexes(repository)
	own = next((i for i in indexes if same_branch(i.branch, branch)), None)
	allowed = kept(repository, indexes, branch_patterns)
	# An index of the query API does not count to the limit, but a review can use it.
	if own and (not own.auto or own in allowed):
		return own.name
	if own is None and len(allowed) < MAX_BRANCH_INDEXES:
		if not create:
			return None
		from guard.indexing.jobs import index_for

		return index_for(repository, branch, auto=True)
	if create:
		frappe.logger("guard").warning(
			f"{repository}: not indexing branch {branch}: {len(allowed)} branches have an index, "
			f"and {MAX_BRANCH_INDEXES} is the limit"
		)
	return None


def ready_index(
	repository: str, branch: str, branch_patterns: list[str] | None, queue: bool = False
) -> str | None:
	"""The completed index of a listed base branch, else None.

	With `queue`, queue a missing or unfinished index on the `long` queue: the first index of a branch
	is a full index, which can take longer than a review may wait. The review then uses the index of
	the default branch, and a later review uses this one.
	"""
	index = index_of(repository, branch, branch_patterns, create=queue)
	if not index:
		return None
	status = db.value("Repository Index", index, "status")
	if status == "completed":
		return index
	# A failed run blocks indexing for an hour, as on the default branch.
	if queue and status not in RUNNING and not recent_failure(index):
		from guard.indexing.jobs import enqueue_index

		enqueue_index(repository, branch)
	return None


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
	"""Every hour: drop the branch indexes that are not wanted, and index again each wanted one whose
	head moved past it.

	The index of the default branch is never dropped here. A failure in one repository does not stop
	the others.
	"""
	repositories = db.rows(
		"""SELECT DISTINCT i.repository FROM `tabRepository Index` i
		JOIN `tabCode Repository` r ON r.name = i.repository
		WHERE i.branch != IFNULL(r.default_branch, '') AND i.name != IFNULL(r.default_index, '')"""
	)
	for (repository,) in repositories:
		try:
			indexes = branch_indexes(repository)
			if indexes:
				_catch_up(repository, indexes)
			db.commit()
		except Exception as error:
			db.rollback()
			frappe.log_error(redact(str(error)), f"Guard branch index catch-up: {repository}")


def _catch_up(repository: str, indexes: list[BranchIndex]) -> None:
	branch_patterns = patterns(repository, fetch=True)
	if branch_patterns is None:
		return  # Not on a guess: a read that failed must not drop an index.
	store = store_for(repository)
	remote = {branch.casefold() for branch in store.remote_branches()}
	if default_branch(repository).casefold() not in remote:
		# An empty or partial answer of the remote. Do not take it for deleted branches.
		frappe.logger("guard").warning(
			f"{repository}: the remote did not list the default branch: no branch index is dropped"
		)
		return
	present = [index for index in indexes if index.branch.casefold() in remote]
	# A branch that is gone does not hold a place under the limit.
	allowed = kept(repository, present, branch_patterns)
	for index in indexes:
		try:
			_catch_up_index(repository, store, index, index in present, index in allowed, branch_patterns)
		except GitError as error:
			# One bad branch must not stop the others.
			frappe.logger("guard").warning(
				f"{repository}: catch-up of the index of branch {index.branch} failed: {redact(str(error))}"
			)


def _catch_up_index(
	repository: str,
	store: GitStore,
	index: BranchIndex,
	present: bool,
	allowed: bool,
	branch_patterns: list[str],
) -> None:
	if not present:
		drop_index(index.name)  # Any index: the branch is gone.
		return
	if index.auto and not allowed:
		drop_index(index.name)  # Not listed now, or past the limit.
		return
	# An index of the query API for an unlisted branch: keep it, and leave it as it is.
	if not index.auto and not listed(repository, index.branch, branch_patterns):
		return
	# As on the default branch: a run in progress or a failed run is not repeated here.
	if index.status != "completed":
		return
	store.fetch_branches(index.branch)
	if store.rev_parse(f"refs/heads/{index.branch}") != index.sha:
		from guard.indexing.jobs import enqueue_index

		enqueue_index(repository, index.branch)


def drop_index(index: str) -> bool:
	"""Delete the rows and the vectors of an index, then the Repository Index.

	Return False and do nothing when the index is a default index, a run holds its lock, or a review
	of a pull request to its branch is queued or running. The next catch-up tries again.

	The status does not matter when the lock is free. A run holds the lock while it writes, so a
	`cloning` or `processing` index without the lock is a run whose worker died. A `submitted` index
	has a job that has not started: when it starts, `run_index` finds no index and stops.
	"""
	row = db.values("Repository Index", index, ("repository", "branch")) or {}
	repository, branch = str(row.get("repository") or ""), str(row.get("branch") or "")
	if not repository or _is_default(repository, index, branch) or _in_review(repository, branch):
		return False
	with index_lock(index) as acquired:
		if not acquired:
			return False  # A run writes it now.
		# A locking read gives the latest committed row. A plain read can give the snapshot of the
		# first query of this transaction (REPEATABLE READ), from before a run completed. A restore
		# of that snapshot would write an old status and sha over a good index.
		before = db.values(
			"Repository Index", index, ("status", "sha", "error", "completed_at"), for_update=True
		)
		if not before:
			return False
		# Reviews and runs that read the index now see a failed index with no sha, and skip it.
		db.set_values(
			"Repository Index",
			index,
			{
				"status": "failed",
				"sha": None,
				"error": "Guard is dropping this index.",
				"completed_at": now_datetime(),
			},
		)
		db.commit()
		deleting = False
		try:
			# A review that started before the commit above can have read the index as it was.
			if _in_review(repository, branch):
				_restore(index, before)
				return False
			deleting = True
			_delete_rows(index)
			frappe.delete_doc("Repository Index", index, ignore_permissions=True, force=True)
			db.commit()
		except Exception:
			db.rollback()
			# Before the delete, put the index back as it was. After it started, committed batches are
			# gone: the old sha would claim rows that do not exist. The index stays failed with no sha,
			# so no review uses it, and the next catch-up drops it again.
			if not deleting:
				_restore(index, before)
			raise
	frappe.logger("guard").info(f"{repository}: dropped the index {index} of branch {branch}")
	return True


def _restore(index: str, before: dict) -> None:
	"""Put back the status, sha, error and end time that the index had before a drop started."""
	if frappe.db.exists("Repository Index", index):
		db.set_values("Repository Index", index, dict(before))
		db.commit()


def _is_default(repository: str, index: str, branch: str) -> bool:
	if frappe.db.exists("Code Repository", {"default_index": index}):
		return True
	return same_branch(branch, default_branch(repository))


def _in_review(repository: str, branch: str) -> bool:
	"""A review of a pull request to the branch is queued or running. It can read the index."""
	return bool(
		db.scalar(
			"""SELECT 1 FROM `tabCode Review` r JOIN `tabPull Request` p ON p.name = r.pull_request
			WHERE r.repository = %s AND p.base_branch = %s AND r.status IN ('Queued', 'Running') LIMIT 1""",
			(repository, branch),
		)
	)


def _delete_rows(index: str) -> None:
	"""Delete in batches, with a commit after each one."""
	for table in GRAPH_TABLES:
		while names := _names(table, index):
			# A constant table name. The values are parameters.
			db.execute(
				f"DELETE FROM `{table}` WHERE name IN %s", (names,)
			)  # nosemgrep: frappe-sql-format-injection
			db.commit()
	vectors = VectorStore()
	while names := _names("tabCode Chunk", index):
		vectors.delete_refs("chunk", list(names))
		db.execute("DELETE FROM `tabCode Chunk` WHERE name IN %s", (names,))
		db.commit()


def _names(table: str, index: str) -> tuple[str, ...]:
	rows = db.rows(
		f"SELECT name FROM `{table}` WHERE `index` = %s LIMIT %s",  # nosemgrep: frappe-sql-format-injection
		(index, DROP_BATCH),
	)
	return tuple(r[0] for r in rows)
