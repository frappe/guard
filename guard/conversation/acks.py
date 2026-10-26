"""Reactions on the comment that mentions Guard: 👀 when Guard accepts it, 👍 when it is done.

A review runs later, in a job. The mention waits on its Pull Request Comment, and the next
review of the pull request that ends finishes it. See docs/spec/07-conversations.md.
"""

from dataclasses import dataclass

import frappe

from guard.providers import adapter_for_repository
from guard.providers.base import ProviderAdapter, ProviderComment, ProviderError
from guard.utils import db


@dataclass
class Ack:
	pull_request: str
	comment_id: str
	target: str  # issue or inline
	eyes: str = ""  # reaction id, to remove it later


def accept(pull_request: str, comment: ProviderComment) -> Ack:
	ack = Ack(pull_request, comment.id, "inline" if comment.kind == "inline" else "issue")
	ack.eyes = _add(ack, "eyes") or ""
	return ack


def done(ack: Ack, failed: bool = False) -> None:
	"""Replace 👀 with 👍, or with 😕 when the work failed."""
	if ack.eyes:
		_remove(ack, ack.eyes)
	_add(ack, "confused" if failed else "+1")


def withdraw(ack: Ack) -> None:
	"""Guard did not take the command after all: remove 👀."""
	if ack.eyes:
		_remove(ack, ack.eyes)


def wait_for_review(ack: Ack) -> None:
	db.set_values("Pull Request Comment", _comment(ack), {"waits_for_review": 1, "eyes_reaction": ack.eyes})


def forget(ack: Ack) -> None:
	db.set_values("Pull Request Comment", _comment(ack), {"waits_for_review": 0, "eyes_reaction": ""})


def review_finished(pull_request: str, failed: bool = False) -> None:
	"""Called when a review ends. Finish every mention that waits for a review of the pull request."""
	# Two reviews can end at the same time. The row locks let only one of them take the mentions.
	rows = db.dict_rows(
		"""SELECT name, provider_comment_id, kind, eyes_reaction FROM `tabPull Request Comment`
		WHERE pull_request = %s AND waits_for_review = 1 FOR UPDATE""",
		(pull_request,),
	)
	if not rows:
		return
	for row in rows:
		db.set_values("Pull Request Comment", row["name"], {"waits_for_review": 0, "eyes_reaction": ""})
	# Saved before the reactions: a job that dies between the two does not react twice.
	db.commit()
	for row in rows:
		target = "inline" if row["kind"] == "inline" else "issue"
		done(
			Ack(pull_request, str(row["provider_comment_id"]), target, str(row["eyes_reaction"] or "")),
			failed,
		)


def _comment(ack: Ack) -> str:
	"""The Pull Request Comment of the mention. The webhook handler stores each comment before it
	handles it, so a row is missing only for a mention that came another way."""
	filters = {"pull_request": ack.pull_request, "provider_comment_id": ack.comment_id}
	if name := db.value("Pull Request Comment", filters, "name"):
		return str(name)
	doc = frappe.get_doc(
		{
			"doctype": "Pull Request Comment",
			**filters,
			"repository": db.value("Pull Request", ack.pull_request, "repository"),
			"kind": ack.target,
		}
	).insert(ignore_permissions=True)
	return str(doc.name)


def _context(ack: Ack) -> tuple[ProviderAdapter, str, int] | None:
	repository = str(db.value("Pull Request", ack.pull_request, "repository") or "")
	adapter = adapter_for_repository(repository) if repository else None
	if not adapter:
		return None
	full_name = str(db.value("Code Repository", repository, "full_name"))
	return adapter, full_name, int(db.value("Pull Request", ack.pull_request, "number") or 0)


def _add(ack: Ack, content: str) -> str | None:
	context = _context(ack)
	if not context:
		return None
	adapter, full_name, number = context
	try:
		return adapter.add_reaction(full_name, number, ack.target, ack.comment_id, content)
	except ProviderError as error:
		frappe.log_error(str(error), "Guard reaction")
		return None


def _remove(ack: Ack, reaction: str) -> None:
	context = _context(ack)
	if not context:
		return
	adapter, full_name, number = context
	try:
		adapter.remove_reaction(full_name, number, ack.target, ack.comment_id, reaction)
	except ProviderError as error:
		frappe.log_error(str(error), "Guard reaction")
