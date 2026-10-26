"""Reactions on the comment that mentions Guard: 👀 when Guard accepts it, 👍 when it is done.

A review runs later, in a job. The mention waits in the cache under the pull request, and the
last review of the pull request finishes it. See docs/spec/07-conversations.md.
"""

import json
from dataclasses import asdict, dataclass

import frappe

from guard.providers import adapter_for_repository
from guard.providers.base import ProviderAdapter, ProviderComment, ProviderError
from guard.utils import cache, db

PENDING_TTL = 24 * 3600


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
	cache.hash_set(_key(ack.pull_request), ack.comment_id, json.dumps(asdict(ack)), PENDING_TTL)


def forget(ack: Ack) -> None:
	cache.hash_take(_key(ack.pull_request), ack.comment_id)


def review_finished(pull_request: str, failed: bool = False) -> None:
	"""Called when a review ends. Finish every mention that waits for a review of the pull request."""
	key = _key(pull_request)
	for comment_id, pending in cache.hash_items(key).items():
		# Two reviews can end at the same time. Only one of them reacts.
		if cache.hash_take(key, comment_id):
			done(Ack(**json.loads(pending)), failed)


def _key(pull_request: str) -> str:
	return f"guard:mentions:{pull_request}"


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
