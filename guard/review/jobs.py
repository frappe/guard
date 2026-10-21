"""Create and queue Code Reviews, and the work that runs after one."""

import frappe
from frappe.utils import add_to_date, now_datetime

from guard.utils import db

QUEUE_BY_TIER = {"apex": "long"}


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
		timeout=7200 if tier == "apex" else 1800,
		job_id=f"guard-review-{review}-{attempt}",
		deduplicate=True,
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		review=review,
		attempt=attempt,
	)


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
