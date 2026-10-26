"""Polling mode: for sites that GitHub webhooks cannot reach (for example a laptop bench).

Every 5 minutes, list open pull requests of enabled repositories and their comments, and
turn what changed into the same events that webhooks give.
"""

import frappe

from guard import settings as guard_settings
from guard.learning import feedback
from guard.providers import adapter_for_repository
from guard.providers.base import ProviderAdapter, ProviderError
from guard.utils import db
from guard.webhooks import handler
from guard.webhooks.events import WebhookEvent

COMMENT_LIMIT = 200


def poll() -> None:
	if not guard_settings.flag("polling_enabled"):
		return
	for repository in frappe.get_all("Code Repository", filters={"reviews_enabled": 1}, pluck="name"):
		try:
			poll_repository(repository)
		except ProviderError as error:
			frappe.log_error(str(error), "Guard polling")
		db.commit()


def poll_repository(repository: str) -> None:
	adapter = adapter_for_repository(repository)
	if not adapter:
		return
	full_name = str(db.value("Code Repository", repository, "full_name"))
	open_numbers = set()
	for info in adapter.list_open_pull_requests(full_name):
		open_numbers.add(info.number)
		known = db.values(
			"Pull Request",
			{"repository": repository, "number": info.number},
			("name", "head_sha", "state", "is_draft"),
		)
		event = WebhookEvent(
			kind="pull_request",
			repository=None,
			namespace=full_name.split("/")[0],
			number=info.number,
			pull_request=info,
		)
		if not known:
			event.action = "opened"
		elif known["head_sha"] != info.head_sha:
			event.action, event.before, event.after = "synchronize", known["head_sha"], info.head_sha
		elif known["is_draft"] and not info.is_draft:
			event.action = "ready"
		elif known["state"] != "open":
			event.action = "reopened"
		else:
			event.action = ""
		if event.action:
			handler._pull_request(repository, event)
		else:
			from guard.review import pull_requests

			pull_requests.upsert(repository, info)
		_poll_comments(repository, full_name, info.number, adapter)
		_poll_threads(repository, full_name, info.number, adapter)
	_closed(repository, open_numbers, adapter, full_name)


def _poll_comments(repository: str, full_name: str, number: int, adapter: ProviderAdapter) -> None:
	pull_request = db.value("Pull Request", {"repository": repository, "number": number}, "name")
	if not pull_request:
		return
	seen = {
		r[0]
		for r in db.rows(
			"SELECT provider_comment_id FROM `tabPull Request Comment` WHERE pull_request = %s",
			(pull_request,),
		)
	}
	for comment in adapter.list_comments(full_name, number)[-COMMENT_LIMIT:]:
		if comment.id in seen:
			continue
		event = WebhookEvent(kind="comment", action="created", number=number, comment=comment)
		handler._comment(repository, event)


def _poll_threads(repository: str, full_name: str, number: int, adapter: ProviderAdapter) -> None:
	"""Resolved and reopened threads of findings that a thread can change. Webhooks send these as events."""
	pull_request = db.value("Pull Request", {"repository": repository, "number": number}, "name")
	if not pull_request:
		return
	findings = {
		str(r[0]): str(r[1])
		for r in db.rows(
			"""SELECT provider_thread_id, status FROM `tabReview Finding`
			WHERE pull_request = %s AND provider_thread_id IS NOT NULL AND provider_thread_id != ''
			AND (status = 'Open' OR (status = 'Dismissed' AND status_reason LIKE %s))""",
			(pull_request, f"{feedback.RESOLVED_BY}%"),
		)
	}
	if not findings:
		return
	for thread in adapter.list_review_threads(full_name, number):
		status = findings.get(thread.thread_id)
		if thread.resolved and status == "Open":
			feedback.thread_changed(str(pull_request), thread.thread_id, thread.resolved_by, resolved=True)
		elif not thread.resolved and status == "Dismissed":
			# The provider does not say who reopened it.
			feedback.thread_changed(str(pull_request), thread.thread_id, "", resolved=False)


def _closed(repository: str, open_numbers: set[int], adapter: ProviderAdapter, full_name: str) -> None:
	for (number,) in db.rows(
		"SELECT number FROM `tabPull Request` WHERE repository = %s AND state = 'open'", (repository,)
	):
		if number in open_numbers:
			continue
		info = adapter.get_pull_request(full_name, int(number))
		handler._pull_request(
			repository,
			WebhookEvent(
				kind="pull_request",
				action="merged" if info.state == "merged" else "closed",
				number=info.number,
				pull_request=info,
			),
		)
