"""Background jobs for indexing."""

import frappe

from guard.git.repository import store_for
from guard.git.store import GitError
from guard.indexing.indexer import RUNNING, IndexBusy, Indexer, IndexFailed, recent_failure
from guard.indexing.vectors import VectorRow, VectorStore
from guard.llm.embeddings import Embedder
from guard.utils import db


def index_for(repository: str, branch: str | None = None) -> str:
	"""The Repository Index of a branch, created when missing."""
	branch = branch or db.value("Code Repository", repository, "default_branch") or "main"
	name = db.value("Repository Index", {"repository": repository, "branch": branch}, "name")
	if name:
		return str(name)
	doc = frappe.get_doc({"doctype": "Repository Index", "repository": repository, "branch": branch})
	doc.insert(ignore_permissions=True)
	return str(doc.name)


def enqueue_index(repository: str, branch: str | None = None, full: bool = False, now: bool = False) -> str:
	index = index_for(repository, branch)
	# Keep a running status. It is a run in progress, or one that died and needs a full run.
	if db.value("Repository Index", index, "status") not in RUNNING:
		frappe.db.set_value("Repository Index", index, "status", "submitted")
	if branch is None or branch == db.value("Code Repository", repository, "default_branch"):
		frappe.db.set_value("Code Repository", repository, "index_status", "Queued")
		from guard.knowledge.builder import repository_changed

		repository_changed(repository)
	frappe.enqueue(
		"guard.indexing.jobs.run_index",
		queue="long",
		timeout=7200,
		job_id=f"guard-index-{index}",
		deduplicate=True,
		enqueue_after_commit=True,
		now=now or bool(frappe.flags.in_test),
		index=index,
		full=full,
	)
	return index


def run_index(index: str, full: bool = False) -> None:
	repository = db.value("Repository Index", index, "repository")
	if db.value("Code Repository", repository, "default_branch") == db.value(
		"Repository Index", index, "branch"
	):
		frappe.db.set_value("Code Repository", repository, "index_status", "Indexing")
		db.commit()
	try:
		Indexer(index, full=full).run()
	except IndexBusy:
		# Another job is indexing the branch now. The next push indexes it again.
		return


def catch_up() -> None:
	"""Every hour: index again each default branch whose head moved past its index.

	A push to the default branch queues an index, but a lost or ignored delivery does not. On 26 Oct
	a deploy lost the deliveries of an hour, and the index of grove stayed 2 commits behind. Reviews
	do not repair it: `ensure_fresh` takes any completed index.
	"""
	rows = db.dict_rows(
		"""SELECT r.name, r.default_branch, i.sha FROM `tabCode Repository` r
		JOIN `tabRepository Index` i ON i.name = r.default_index
		WHERE i.status = 'completed' AND i.branch = r.default_branch AND IFNULL(i.sha, '') != ''"""
	)
	for row in rows:
		repository, branch = str(row["name"]), str(row["default_branch"])
		try:
			store = store_for(repository)
			store.fetch_branches(branch)
			head = store.rev_parse(f"refs/heads/{branch}")
		except GitError as error:
			frappe.log_error(str(error), f"Guard index catch-up: {repository}")
			continue
		if head != row["sha"]:
			enqueue_index(repository)
		db.commit()


def ensure_fresh(repository: str, sha: str | None = None) -> str:
	"""Make the default index cover `sha` (or the branch head). Run inline.

	Raise IndexBusy when another job is indexing it now, and IndexFailed when the run fails or
	failed in the last hour.
	"""
	index = index_for(repository)
	row = db.values("Repository Index", index, ("status", "sha"))
	if row and row["status"] == "completed" and (sha is None or row["sha"] == sha):
		return index
	if error := recent_failure(index):
		raise IndexFailed(error)
	try:
		Indexer(index).run()
	except IndexBusy:
		raise
	except Exception as error:
		raise IndexFailed(str(error)) from error
	return index


def reembed_all() -> None:
	"""Embed every chunk again, after the embedding model or dimension changed."""
	embedder = Embedder()
	store = VectorStore()
	last = ""
	while True:
		rows = db.rows(
			"SELECT name, repository, content FROM `tabCode Chunk` WHERE name > %s ORDER BY name LIMIT 256",
			(last,),
		)
		if not rows:
			break
		vectors = embedder.embed([row[2] or "" for row in rows])
		store.upsert(
			[VectorRow(row[0], "chunk", row[0], row[1], v) for row, v in zip(rows, vectors, strict=True)]
		)
		last = rows[-1][0]
		db.commit()
