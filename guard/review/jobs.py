"""Create and queue Code Reviews, and the work that runs after one."""

from typing import Any

import frappe
from frappe.utils import add_to_date, now_datetime, time_diff_in_seconds
from frappe.utils.background_jobs import is_job_enqueued

from guard.utils import db

QUEUE_BY_TIER = {"apex": "long"}
DEAD_AFTER = 600  # seconds past the job timeout
# Stages that post nothing yet. A review whose job stopped in one of them can start again.
BEFORE_PUBLISH = ("prepare", "start", "index", "context", "review", "verify", "filter")
MAX_ATTEMPT = 5  # restarts and retries of one review, together
RESTARTED = "restarting: the worker stopped during the review"


def create_review(
	pull_request: str,
	trigger: str,
	requested_by: str = "",
	requested_tier: str = "",
	instructions: str = "",
	user: str | None = None,
) -> str:
	"""Cancel older reviews of the pull request, create a new one and queue it."""
	head = db.value("Pull Request", pull_request, "head_sha")
	repository = db.value("Pull Request", pull_request, "repository")
	for name in frappe.get_all(
		"Code Review",
		filters={"pull_request": pull_request, "status": ["in", ["Queued", "Running"]]},
		pluck="name",
	):
		frappe.db.set_value("Code Review", name, "cancel_requested", 1)
	doc = frappe.get_doc(
		{
			"doctype": "Code Review",
			"pull_request": pull_request,
			"repository": repository,
			"source": "pull_request",
			"status": "Queued",
			"trigger": trigger,
			"requested_by": requested_by,
			"requested_tier": requested_tier or "",
			"instructions": instructions,
			"head_sha": head,
			"triggered_by_user": user,
		}
	).insert(ignore_permissions=True)
	enqueue_review(str(doc.name), tier=requested_tier)
	return str(doc.name)


def enqueue_review(review: str, delay: int = 0, attempt: int = 0, tier: str = "") -> None:
	"""Queue a review now, or mark it to resume after `delay` seconds."""
	if delay and not frappe.flags.in_test:
		db.set_values(
			"Code Review",
			review,
			{"attempt": attempt, "retry_at": add_to_date(now_datetime(), seconds=delay)},
		)
		return
	frappe.enqueue(
		"guard.review.pipeline.run_review",
		queue=QUEUE_BY_TIER.get(tier, "default"),
		timeout=job_timeout(tier),
		job_id=job_id(review, attempt),
		deduplicate=True,
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		review=review,
		attempt=attempt,
	)


def job_timeout(tier: str) -> int:
	"""Seconds a review job may run. A new review has no tier yet, and the gate can make it apex."""
	return 1800 if tier in ("base", "plus") else 7200


def job_id(review: str, attempt: int) -> str:
	return f"guard-review-{review}-{attempt}"


def fail_dead_reviews() -> None:
	"""Every 5 minutes: start again a running review whose job stopped, for example in a deploy.

	A review that had started to post, or that stopped too often, fails: else it stays Running,
	and its pull request shows "Guard is reviewing" for ever.
	"""
	from guard.review.pipeline import ReviewPipeline

	rows = frappe.get_all(
		"Code Review",
		filters={"status": "Running"},
		fields=["name", "attempt", "tier", "started_at", "stage", "cancel_requested"],
	)
	for row in rows:
		attempt = row.attempt or 0
		alive = is_job_enqueued(job_id(row.name, attempt))
		age = time_diff_in_seconds(now_datetime(), row.started_at) if row.started_at else 0
		if alive and age < job_timeout(row.tier or "") + DEAD_AFTER:
			continue
		if row.cancel_requested and not alive:
			db.set_values("Code Review", row.name, {"status": "Cancelled", "completed_at": now_datetime()})
			continue
		if not alive and row.stage in BEFORE_PUBLISH and attempt + 1 < MAX_ATTEMPT:
			db.set_values("Code Review", row.name, {"status": "Queued", "stage": RESTARTED})
			enqueue_review(row.name, attempt=attempt + 1, tier=row.tier or "")
			continue
		pipeline = ReviewPipeline(row.name)
		pipeline.fail("The review job stopped before the review finished, for example in a restart.")
		pipeline.acknowledge()


def retry_review(review: str) -> bool:
	"""Run a failed or cancelled review again, from the start. False when it is in another state."""
	row = db.values("Code Review", review, ("status", "tier")) or {}
	if row.get("status") not in ("Failed", "Cancelled"):
		return False
	db.set_values(
		"Code Review",
		review,
		{
			"status": "Queued",
			"stage": "",
			"error": None,
			"attempt": 0,
			"retry_at": None,
			"completed_at": None,
			"cancel_requested": 0,
		},
	)
	enqueue_review(review, attempt=0, tier=str(row.get("tier") or ""))
	return True


def resume_waiting() -> None:
	"""Every minute: queue reviews whose retry time has passed."""
	rows = frappe.get_all(
		"Code Review",
		filters={"status": "Queued", "retry_at": ["<=", now_datetime()]},
		fields=["name", "attempt", "tier"],
	)
	for row in rows:
		frappe.db.set_value("Code Review", row.name, "retry_at", None)
		enqueue_review(row.name, attempt=row.attempt or 0, tier=row.tier or "")


STOPPED = "Guard stopped before it finished this review. Ask for a new review if you need one."


def close_stuck_checks(days: int = 1) -> int:
	"""Every hour: complete the checks that say "Guard is reviewing" on a commit no review runs on.

	A killed job, or an id that the cache lost, leaves one. Return how many were closed.
	"""
	from guard.providers import adapter_for_repository

	rows = db.dict_rows(
		"""SELECT r.repository, c.full_name, r.head_sha FROM `tabCode Review` r
		JOIN `tabCode Repository` c ON c.name = r.repository
		WHERE r.source = 'pull_request' AND r.head_sha IS NOT NULL AND r.head_sha != '' AND r.creation > %s
		GROUP BY r.repository, c.full_name, r.head_sha
		HAVING SUM(r.status IN ('Queued', 'Running')) = 0""",
		(add_to_date(now_datetime(), days=-days),),
	)
	adapters: dict[str, Any] = {}
	closed = 0
	for row in rows:
		repository = str(row["repository"])
		if repository not in adapters:
			adapters[repository] = adapter_for_repository(repository)
		adapter = adapters[repository]
		if not adapter:
			continue
		try:
			closed += adapter.close_open_checks(str(row["full_name"]), str(row["head_sha"]), STOPPED)
		except Exception as error:
			frappe.log_error(str(error)[:2000], "Guard checks")
	return closed


def after_review(review: str) -> None:
	"""Auto-approve and tour, after the review is saved."""
	from guard.conversation.tour import build_tour
	from guard.learning.approve import maybe_approve

	maybe_approve(review)
	build_tour(review)
