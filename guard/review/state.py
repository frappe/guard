"""The status of a Code Review, and what users see of it on the provider and in Desk.

The Code Review row is the one source of truth. A user action changes it in the request, and the
provider check and the Desk views follow at once, when the transaction commits. A background job
that runs the review later only moves the status with `move`, which reads it under a row lock.
So the job can never undo what the user did. See docs/spec/05-review-engine.md#status.
"""

from typing import Any

import frappe
from frappe.utils import get_fullname, now_datetime

from guard.providers import adapter_for_repository
from guard.providers.base import ProviderAdapter
from guard.utils import db
from guard.utils.redact import redact

ACTIVE = ("Queued", "Running")
# Guard posts the review in these stages. A cancel then would leave half a review on the provider.
POSTING = ("publish", "after")
REPLACED = "a newer review replaced it"
CLOSED = "the pull request was closed"
QUEUED_TEXT = "Guard will review this commit soon"
STOPPED_PREFIX = "stopped: "


class NotActive(Exception):
	"""The review ended, or it posts its result now, so it cannot change."""


def move(review: str, values: dict[str, Any], allowed: tuple[str, ...] = ACTIVE) -> bool:
	"""Write `values` when the review is in one of the `allowed` statuses.

	Return False, and write nothing, when it is not. Jobs use this for every status they set.
	"""
	if db.value_for_update("Code Review", review, "status") not in allowed:
		return False
	db.set_values("Code Review", review, values)
	return True


def cancel(review: str, reason: str, by: str | None = None) -> None:
	"""Cancel the review now. The provider check and Desk show it as soon as this commits.

	The job of the review, when one runs, stops at its next step. Raise NotActive when the review
	ended or posts its result now.
	"""
	row = db.values("Code Review", review, ("status", "stage"), for_update=True) or {}
	if row.get("status") not in ACTIVE:
		raise NotActive(review)
	if row.get("status") == "Running" and row.get("stage") in POSTING:
		raise NotActive(review)
	db.set_values(
		"Code Review",
		review,
		{
			"status": "Cancelled",
			"cancel_requested": 1,
			"cancelled_by": by,
			"completed_at": now_datetime(),
			"retry_at": None,
			"stage": STOPPED_PREFIX + reason,
		},
	)
	_on_provider("show_cancelled", review)
	notify(review)


def _on_provider(step: str, review: str) -> None:
	"""Show the change on the provider when the transaction commits. A bulk action sets
	`guard_provider_in_job`: then a short job does it, because a request that makes hundreds of
	provider calls passes the request timeout."""
	if frappe.flags.guard_provider_in_job:
		frappe.enqueue(
			f"guard.review.state.{step}",
			queue="short",
			enqueue_after_commit=True,
			now=bool(frappe.flags.in_test),
			review=review,
		)
	else:
		action = show_cancelled if step == "show_cancelled" else _show_queued
		db.after_commit(lambda: action(review))


def cancel_reason(user: str) -> str:
	return f"{get_fullname(user)} cancelled it"


def show_cancelled(review: str) -> None:
	"""Show on the provider that the review stopped: end its check, remove 👀, answer mentions.

	The request that cancels calls this, and so does the job when it stops: a job that was between
	two steps can have set the check again. Each step does nothing the second time.
	"""
	try:
		_show_cancelled(review)
	except Exception as error:
		# The cancel is saved. The check of a stopped review is also closed every hour.
		frappe.log_error(redact(str(error)), "Guard cancel")
	from guard.review import checkpoint

	checkpoint.delete(review)


def _show_cancelled(review: str) -> None:
	from guard.conversation import acks
	from guard.review.publish import remove_eyes

	row = db.values("Code Review", review, ("pull_request", "head_sha", "stage", "check_run_id", "creation"))
	if not row or not row["pull_request"]:
		return
	pull_request = str(row["pull_request"])
	provider = _provider(pull_request)
	if provider:
		adapter, full_name, number = provider
		# GitHub gives each user one 👀 on the pull request, so the reviews share it. A review that runs
		# removes it when it ends.
		if not review_runs(pull_request, review):
			remove_eyes(adapter, full_name, number, review)
		sha = str(row["head_sha"] or "")
		# The check is shared by the reviews of a commit. A newer review of it, or one that runs, owns it.
		if (
			sha
			and not newer_review_of(pull_request, sha, row["creation"])
			and not other_review_runs(pull_request, sha, review)
		):
			reason = str(row["stage"] or "").removeprefix(STOPPED_PREFIX) or REPLACED
			end_check(
				adapter,
				full_name,
				pull_request,
				sha,
				f"Guard stopped this review: {reason}.",
				review,
				str(row["check_run_id"] or ""),
			)
	# A mention that asked for this review waits for the newer one, when there is one.
	if not newer_review_runs(pull_request, review, row["creation"]):
		acks.review_finished(pull_request, failed=True)


def queued(review: str, status_check: bool) -> None:
	"""Show the new review at once: a queued check on its commit, and the review in Desk."""
	notify(review)
	if status_check:
		_on_provider("_show_queued", review)


def _show_queued(review: str) -> None:
	from guard.review.publish import set_status

	row = db.values("Code Review", review, ("pull_request", "head_sha", "check_run_id", "status"))
	if not row or not row["pull_request"] or not row["head_sha"] or row["status"] != "Queued":
		return
	try:
		provider = _provider(str(row["pull_request"]))
		if not provider:
			return
		adapter, full_name, _number = provider
		check_id = set_status(
			adapter,
			full_name,
			str(row["head_sha"]),
			"queued",
			QUEUED_TEXT,
			_link(review),
			str(row["check_run_id"] or ""),
		)
	except Exception as error:
		# The job sets the check again when it starts the review.
		frappe.log_error(redact(str(error)), "Guard status")
		return
	if check_id and check_id != row["check_run_id"]:
		frappe.db.set_value("Code Review", review, "check_run_id", check_id, update_modified=False)


def finding_changed(finding: str, sync_thread: bool = True) -> None:
	"""A user dismissed or reopened a finding. Resolve or reopen its thread, and count it on the check.

	`sync_thread` is False when the change came from the thread on the provider.
	"""
	pull_request = db.value("Review Finding", finding, "pull_request")
	if not pull_request:
		return
	frappe.publish_realtime(
		"guard_review_updated",
		{"review": None, "pull_request": pull_request},
		doctype="Pull Request",
		docname=pull_request,
		after_commit=True,
	)
	db.after_commit(lambda: _show_finding(finding, str(pull_request), sync_thread))


def _show_finding(finding: str, pull_request: str, sync_thread: bool = True) -> None:
	from guard.providers.base import ProviderError

	try:
		provider = _provider(pull_request)
		if not provider:
			return
		adapter, full_name, number = provider
		row = db.values("Review Finding", finding, ("status", "provider_thread_id")) or {}
		if sync_thread and row.get("provider_thread_id"):
			try:
				if row["status"] == "Open":
					adapter.unresolve_thread(full_name, number, str(row["provider_thread_id"]))
				else:
					adapter.resolve_thread(full_name, number, str(row["provider_thread_id"]))
			except ProviderError:
				pass  # Not every provider has threads. The check below still counts it.
		recount_check(adapter, full_name, pull_request)
	except Exception as error:
		# The status is saved. The next review sets the check again.
		frappe.log_error(redact(str(error)), "Guard finding")


def recount_check(adapter: ProviderAdapter, full_name: str, pull_request: str) -> None:
	"""Set the check of the head again, with the open findings now. Only when the last review of the
	head completed: else a review runs and sets the check when it ends, or the check shows a failure."""
	head = str(db.value("Pull Request", pull_request, "head_sha") or "")
	latest = _reviews_of(pull_request, head, "")
	if latest and latest[0]["status"] == "Completed" and latest[0]["check_run_id"]:
		severities = [
			str(row[0])
			for row in db.rows(
				"SELECT severity FROM `tabReview Finding` WHERE pull_request = %s AND status = 'Open'",
				(pull_request,),
			)
		]
		_show_result(
			adapter, full_name, pull_request, head, latest[0], str(latest[0]["check_run_id"]), severities
		)


def keep_thread_open(pull_request: str, thread_id: str, body: str) -> None:
	"""A person resolved the thread of a finding that stays open. Reopen the thread and reply `body`,
	so the provider does not show it closed while the finding still fails the check."""
	from guard.providers.base import ProviderComment

	try:
		provider = _provider(pull_request)
		if not provider:
			return
		adapter, full_name, number = provider
		adapter.unresolve_thread(full_name, number, thread_id)
		adapter.reply_to_comment(
			full_name,
			number,
			ProviderComment(id=thread_id, kind="inline", author="", body="", thread_id=thread_id),
			body,
		)
	except Exception as error:
		# The finding stays open either way. Polling and the next commit must go on.
		frappe.log_error(redact(str(error)), "Guard finding")


def end_check(
	adapter: ProviderAdapter,
	full_name: str,
	pull_request: str,
	sha: str,
	text: str,
	review: str,
	check_id: str,
) -> None:
	"""End the check of a review that stopped. A completed review of the commit gets its result back:
	the stopped review had set the check of that result to queued."""
	from guard.review.publish import set_status

	if not check_id:
		# A review that never set a check must not add one: only end the checks in progress.
		adapter.close_open_checks(full_name, sha, text)
		return
	completed = _reviews_of(pull_request, sha, "Completed")
	if completed:
		_show_result(adapter, full_name, pull_request, sha, completed[0], check_id)
	else:
		set_status(adapter, full_name, sha, "cancelled", text, _link(review), check_id)


def _show_result(
	adapter: ProviderAdapter,
	full_name: str,
	pull_request: str,
	sha: str,
	review: dict[str, Any],
	check_id: str,
	open_severities: list[str] | None = None,
) -> None:
	"""The result of a completed review on its check. With `open_severities`, the findings that are open
	now set the score: a person closed or reopened one, and the score of the review counts the old ones.
	Else closing every finding could never pass the check."""
	from guard.review.findings import score_cap
	from guard.review.publish import open_finding_count, set_status

	if open_severities is None:
		score = int(review["confidence_score"] or 0)
		open_count = open_finding_count(pull_request)
	else:
		score, open_count = score_cap([], open_severities), len(open_severities)
	set_status(
		adapter,
		full_name,
		sha,
		"success" if score == 5 and not open_count else "failure",
		f"{score}/5 · {open_count} open finding(s)",
		_link(str(review["name"])),
		check_id,
	)


def _reviews_of(pull_request: str, sha: str, status: str) -> list[dict[str, Any]]:
	"""The newest review of the commit, of any status when `status` is blank."""
	return db.dict_rows(
		"""SELECT name, status, confidence_score, check_run_id FROM `tabCode Review`
		WHERE pull_request = %s AND head_sha = %s AND (%s = '' OR status = %s)
		ORDER BY creation DESC LIMIT 1""",
		(pull_request, sha, status, status),
	)


def check_of(pull_request: str, head_sha: str) -> str:
	"""The id of Guard's check on the commit, from the last review of it that has one."""
	return str(
		db.scalar(
			"""SELECT check_run_id FROM `tabCode Review` WHERE pull_request = %s AND head_sha = %s
			AND IFNULL(check_run_id, '') != '' ORDER BY creation DESC LIMIT 1""",
			(pull_request, head_sha),
		)
		or ""
	)


def notify(review: str) -> None:
	"""Tell the open Desk views of the review and of its pull request to load it again."""
	pull_request = db.value("Code Review", review, "pull_request")
	data = {"review": review, "pull_request": pull_request}
	# Document rooms: only people who have the review or the pull request open get the event.
	frappe.publish_realtime(
		"guard_review_updated", data, doctype="Code Review", docname=review, after_commit=True
	)
	if pull_request:
		frappe.publish_realtime(
			"guard_review_updated", data, doctype="Pull Request", docname=pull_request, after_commit=True
		)


def other_review_runs(pull_request: str, head_sha: str, review: str) -> bool:
	"""Another review of the commit is queued or running."""
	return bool(
		db.scalar(
			"""SELECT COUNT(*) FROM `tabCode Review` WHERE pull_request = %s AND head_sha = %s
			AND name != %s AND status IN ('Queued', 'Running')""",
			(pull_request, head_sha, review),
		)
	)


def review_runs(pull_request: str, review: str) -> bool:
	"""Another review of the pull request is queued or running."""
	return bool(
		db.scalar(
			"""SELECT COUNT(*) FROM `tabCode Review` WHERE pull_request = %s AND name != %s
			AND status IN ('Queued', 'Running')""",
			(pull_request, review),
		)
	)


def newer_review_of(pull_request: str, head_sha: str, creation: Any) -> bool:
	"""A newer review of the commit exists, in any status. Its check result wins."""
	return bool(
		db.scalar(
			"""SELECT COUNT(*) FROM `tabCode Review` WHERE pull_request = %s AND head_sha = %s
			AND creation > %s""",
			(pull_request, head_sha, creation),
		)
	)


def newer_review_runs(pull_request: str, review: str, creation: Any) -> bool:
	"""A newer review of the pull request is queued or runs."""
	return bool(
		db.scalar(
			"""SELECT COUNT(*) FROM `tabCode Review` WHERE pull_request = %s AND name != %s
			AND creation > %s AND status IN ('Queued', 'Running')""",
			(pull_request, review, creation),
		)
	)


def _provider(pull_request: str) -> tuple[ProviderAdapter, str, int] | None:
	repository = str(db.value("Pull Request", pull_request, "repository") or "")
	adapter = adapter_for_repository(repository) if repository else None
	if not adapter:
		return None
	full_name = str(db.value("Code Repository", repository, "full_name"))
	return adapter, full_name, int(db.value("Pull Request", pull_request, "number") or 0)


def _link(review: str) -> str:
	from guard.review import links

	return links.code_review(review)
