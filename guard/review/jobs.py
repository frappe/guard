"""Create and queue Code Reviews, and the work that runs after one."""

import frappe
from frappe.utils import add_to_date, now_datetime, time_diff_in_seconds
from frappe.utils.background_jobs import is_job_enqueued

from guard.utils import db

QUEUE_BY_TIER = {"apex": "long"}
DEAD_AFTER = 600  # seconds past the job timeout


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
	return 7200 if tier == "apex" else 1800


def job_id(review: str, attempt: int) -> str:
	return f"guard-review-{review}-{attempt}"


def fail_dead_reviews() -> None:
	"""Every 5 minutes: fail a running review whose job stopped, for example in a restart.

	Else it stays Running, and its pull request shows "Guard is reviewing" for ever.
	"""
	from guard.review.pipeline import ReviewPipeline

	rows = frappe.get_all(
		"Code Review", filters={"status": "Running"}, fields=["name", "attempt", "tier", "started_at"]
	)
	for row in rows:
		alive = is_job_enqueued(job_id(row.name, row.attempt or 0))
		age = time_diff_in_seconds(now_datetime(), row.started_at) if row.started_at else 0
		if alive and age < job_timeout(row.tier or "") + DEAD_AFTER:
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


def after_review(review: str) -> None:
	"""Auto-approve and tour, after the review is saved."""
	from guard.conversation.tour import build_tour
	from guard.learning.approve import maybe_approve

	maybe_approve(review)
	build_tour(review)
