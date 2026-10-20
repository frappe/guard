"""Background jobs for indexing."""

import frappe

from guard.indexing.indexer import Indexer
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
	frappe.db.set_value("Repository Index", index, "status", "submitted")
	if branch is None or branch == db.value("Code Repository", repository, "default_branch"):
		frappe.db.set_value("Code Repository", repository, "index_status", "Queued")
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
		frappe.db.commit()
	Indexer(index, full=full).run()


def ensure_fresh(repository: str, sha: str | None = None) -> str:
	"""Make the default index cover `sha` (or the branch head). Run inline."""
	index = index_for(repository)
	row = db.values("Repository Index", index, ("status", "sha"))
	if row and row["status"] == "completed" and (sha is None or row["sha"] == sha):
		return index
	Indexer(index).run()
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
		frappe.db.commit()
