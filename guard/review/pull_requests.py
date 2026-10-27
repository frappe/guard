"""Keep Pull Request records in step with GitHub."""

from typing import TYPE_CHECKING, cast

import frappe

from guard.providers import adapter_for_repository
from guard.providers.base import PullRequestInfo
from guard.utils import db
from guard.utils.timeutil import parse_iso

if TYPE_CHECKING:
	from guard.guard.doctype.pull_request.pull_request import PullRequest


def find(repository: str, number: int, for_update: bool = False) -> str | None:
	"""The name of the pull request record. `for_update` locks the row until the transaction ends."""
	row = db.values(
		"Pull Request", {"repository": repository, "number": number}, ("name",), for_update=for_update
	)
	return str(row["name"]) if row else None


def upsert(repository: str, info: PullRequestInfo) -> "PullRequest":
	"""Store the pull request.

	GitHub sends some events of one pull request in the same second, and each one runs in its own
	job. Each job locks the row, so the jobs write one after the other and not over each other. The
	lock lasts until the transaction ends: commit before slow work.
	"""
	try:
		return _upsert(repository, info, find(repository, info.number))
	except frappe.UniqueValidationError:
		# Another job inserted the pull request after `find`. Update that row. A plain read can miss
		# it in an older snapshot, a locking read cannot.
		frappe.clear_last_message()
		return _upsert(repository, info, find(repository, info.number, for_update=True))


def _upsert(repository: str, info: PullRequestInfo, name: str | None) -> "PullRequest":
	# Only an existing row is locked. A locking read of a missing key locks a gap, and two jobs
	# that both hold the gap and insert deadlock. The unique key orders inserts instead.
	doc = cast(
		"PullRequest",
		frappe.get_doc("Pull Request", name, for_update=True) if name else frappe.new_doc("Pull Request"),
	)
	doc.update(
		{
			"repository": repository,
			"number": info.number,
			"title": info.title or f"#{info.number}",
			"description": info.description,
			"author": info.author,
			"state": info.state,
			"is_draft": int(info.is_draft),
			"base_branch": info.base_branch,
			"head_branch": info.head_branch,
			"web_url": info.web_url,
			"head_clone_url": info.head_clone_url,
			"labels": frappe.as_json(info.labels),
			"provider_node_id": info.node_id or doc.provider_node_id,
		}
	)
	if info.head_sha:
		doc.head_sha = info.head_sha
	if info.base_sha:
		doc.base_sha = info.base_sha
	for field in ("opened_at", "merged_at", "closed_at"):
		if moment := parse_iso(getattr(info, field)):
			doc.set(field, moment)
	for field in ("additions", "deletions", "changed_files"):
		if getattr(info, field):
			doc.set(field, getattr(info, field))
	doc.save(ignore_permissions=True)
	return doc


def refresh(repository: str, number: int) -> "PullRequest":
	"""Read the pull request from GitHub and store it."""
	adapter = adapter_for_repository(repository)
	if not adapter:
		raise frappe.ValidationError("The repository has no code provider")
	full_name = str(db.value("Code Repository", repository, "full_name"))
	return upsert(repository, adapter.get_pull_request(full_name, number))


def ensure(repository: str, number: int) -> "PullRequest":
	name = find(repository, number)
	if name:
		return cast("PullRequest", frappe.get_doc("Pull Request", name))
	return refresh(repository, number)
