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
from guard.config.resolver import ConfigResolver
from guard.git.repository import default_branch, default_head, fetch_default, store_for
from guard.git.store import GitError
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
# Only these can be dropped. Any other status is a queued or running job that writes the rows.
DROPPABLE = ("completed", "failed")
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
		frappe.logger("guard").warning(f"{repository}: cannot read {SETTING}: {redact(str(error))}")
		return None
	return list(resolver.resolve("").settings.get(SETTING) or [])


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
	allowed = kept(repository, indexes, branch_patterns)
	for index in indexes:
		if index.branch.casefold() not in remote:
			drop_index(index.name)  # Any index: the branch is gone.
			continue
		if index.auto and index not in allowed:
			drop_index(index.name)  # Not listed now, or past the limit.
			continue
		# An index of the query API for an unlisted branch: keep it, and leave it as it is.
		if not index.auto and not listed(repository, index.branch, branch_patterns):
			continue
		# As on the default branch: a run in progress or a failed run is not repeated here.
		if index.status != "completed":
			continue
		store.fetch_branches(index.branch)
		if store.rev_parse(f"refs/heads/{index.branch}") != index.sha:
			from guard.indexing.jobs import enqueue_index

			enqueue_index(repository, index.branch)


def drop_index(index: str) -> bool:
	"""Delete the rows and the vectors of an index, then the Repository Index.

	Return False and do nothing when the index is a default index, has a queued or running job, or a
	review of a pull request to its branch is queued or running. The next catch-up tries again.
	"""
	row = db.values("Repository Index", index, ("repository", "branch")) or {}
	repository, branch = str(row.get("repository") or ""), str(row.get("branch") or "")
	if not repository or _is_default(repository, index, branch) or _in_review(repository, branch):
		return False
	with index_lock(index) as acquired:
		if not acquired:
			return False  # A run writes it now.
		# Read again under the lock: a job can have started since the read above.
		if db.value("Repository Index", index, "status") not in DROPPABLE:
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
		_delete_rows(index)
		frappe.delete_doc("Repository Index", index, ignore_permissions=True, force=True)
		db.commit()
	frappe.logger("guard").info(f"{repository}: dropped the index {index} of branch {branch}")
	return True


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
