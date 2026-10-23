"""A circuit breaker for the AI gateway.

When the gateway keeps failing, reviews wait without using up their retries, admins hear about
it once, and the reviews that failed during the outage run again when the gateway answers.
"""

import time

import frappe
from frappe.utils import now_datetime

from guard.utils import cache, db

THRESHOLD = 3  # outages in a row that open the breaker
PROBE_AFTER = 600  # seconds the breaker stays open before a review tries the gateway again
FAILURES = "guard:gateway:failures"
OPEN = "guard:gateway:open"
OUTAGE = "guard:gateway:outage"
FAILED_PREFIX = "AI gateway down"  # the start of the error of a review that failed in an outage


def wait_seconds() -> int:
	"""Seconds until a review may try the gateway again. 0 when the breaker is closed."""
	until = cache.get(OPEN)
	if not until:
		return 0
	return max(1, int(float(until) - time.time()))


def record_failure(reason: str) -> None:
	"""One more outage. The third in a row opens the breaker."""
	failures = int(cache.get(FAILURES) or 0) + 1
	cache.set(FAILURES, failures, 3600)
	if failures < THRESHOLD:
		return
	cache.set(OPEN, time.time() + PROBE_AFTER, PROBE_AFTER)
	if cache.get(OUTAGE):
		return
	cache.set(OUTAGE, {"since": str(now_datetime()), "reason": reason[:500]}, 7 * 86400)
	_notify_admins(reason)


def record_success() -> None:
	"""The gateway answered. Close the breaker, and run the reviews the outage failed."""
	if cache.get(FAILURES) is None and cache.get(OUTAGE) is None:
		return
	cache.delete(FAILURES)
	cache.delete(OPEN)
	outage = cache.get(OUTAGE)
	if not isinstance(outage, dict):
		return
	cache.delete(OUTAGE)
	rerun_failed(str(outage.get("since") or ""))


def rerun_failed(since: str) -> list[str]:
	"""Queue again the reviews that failed on the gateway since `since`."""
	from guard.review.jobs import retry_review

	names = [
		str(row[0])
		for row in db.rows(
			"SELECT name FROM `tabCode Review` WHERE status = 'Failed' AND error LIKE %s AND completed_at >= %s",
			(f"{FAILED_PREFIX}%", since),
		)
	]
	for name in names:
		retry_review(name)
	return names


def _notify_admins(reason: str) -> None:
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
	from frappe.utils.user import get_users_with_role

	users = get_users_with_role("Guard Admin") or get_users_with_role("System Manager")
	if not users:
		return
	enqueue_create_notification(
		users,
		{
			"type": "Alert",
			"subject": "Guard paused reviews: the AI gateway is down",
			"email_content": (
				"Reviews wait until the gateway answers again, then the ones that failed run again.\n\n"
				f"Last error: {frappe.utils.escape_html(reason[:500])}"
			),
			"document_type": "Guard Settings",
			"document_name": "Guard Settings",
			"from_user": "Administrator",
		},
	)
