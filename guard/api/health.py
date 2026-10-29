"""The state of Guard in one view: queues, workers, reviews, the AI providers and the indexes, and
how reviews ran over a period (`guard.review.metrics`).

The `guard-health` page shows `status()`. `check_failure_rate()` runs every 15 minutes and
notifies admins when many reviews fail.
"""

from typing import Any

import frappe
from frappe.utils import add_to_date, cint, now_datetime

from guard.indexing.indexer import RUNNING
from guard.llm import breaker
from guard.review import metrics
from guard.utils import db, notify

QUEUES = ("short", "default", "long")
STUCK_AFTER = 2  # hours an index may run before it counts as stuck
ALERT_RATE = 0.2  # failed share of the reviews in the last hour that notifies admins
ALERT_MIN = 5  # reviews in the hour before the rate counts


@frappe.whitelist()
def status(hours: int = 24) -> dict[str, Any]:
	"""The state now, and the reviews of the last `hours` hours: one of `metrics.PERIODS`."""
	frappe.only_for(["Guard Admin", "System Manager"])
	hours = cint(hours) if cint(hours) in metrics.PERIODS else 24
	return {
		"queues": _queues(),
		"reviews": _reviews(hours=hours),
		"failures": _failures(hours=hours),
		"ai_providers": _ai_providers(),
		"indexes": _indexes(),
		"failure_rate": failure_rate(),
		"metrics": metrics.metrics(hours),
	}


@frappe.whitelist(methods=["POST"])
def close_stuck_checks(days: int = 7) -> int:
	"""Complete the checks of the last `days` days that say "Guard is reviewing" on a commit that
	no review runs on. The hourly job does the same for the last day."""
	frappe.only_for(["Guard Admin", "System Manager"])
	from guard.review.jobs import close_stuck_checks as close

	return close(min(max(cint(days), 1), 30))


def failure_rate() -> dict[str, Any]:
	"""Reviews that ended in the last hour, and the share that failed."""
	row = (
		db.dict_rows(
			"""SELECT COUNT(*) AS ended, SUM(status = 'Failed') AS failed FROM `tabCode Review`
			WHERE status IN ('Completed', 'Failed') AND completed_at > %s""",
			(add_to_date(now_datetime(), hours=-1),),
		)
		or [{}]
	)[0]
	ended, failed = int(row.get("ended") or 0), int(row.get("failed") or 0)
	return {"ended": ended, "failed": failed, "rate": round(failed / ended, 2) if ended else 0.0}


def check_failure_rate() -> None:
	"""Every 15 minutes: notify admins, at most once an hour, when many reviews fail."""
	rate = failure_rate()
	if rate["ended"] < ALERT_MIN or rate["rate"] <= ALERT_RATE:
		return
	causes = "\n".join(f"- {row['count']}: {row['cause']}" for row in _failures(hours=1)[:5])
	notify.admins(
		f"Guard: {rate['failed']} of {rate['ended']} reviews failed in the last hour",
		f"Open the Guard Health page for details.\n\n{causes}",
		key="failure-rate",
	)


def queue_names() -> list[str]:
	"""The shared queues, and the dedicated review queues that the bench has."""
	from frappe.utils.background_jobs import get_queues_timeout

	from guard.review.jobs import REVIEW_QUEUES

	configured = get_queues_timeout()
	return [*QUEUES, *(name for name in REVIEW_QUEUES if name in configured)]


def _queues() -> list[dict[str, Any]]:
	from frappe.utils.background_jobs import get_queue, get_workers
	from rq.registry import StartedJobRegistry

	names = queue_names()
	try:
		workers = get_workers()
		rows = []
		for name in names:
			queue = get_queue(name)
			rows.append(
				{
					"queue": name,
					"queued": queue.count,
					"running": StartedJobRegistry(queue=queue).count,
					"workers": sum(1 for w in workers if queue.name in w.queue_names()),
				}
			)
		return rows
	except Exception as error:  # Redis down is itself the answer
		return [{"queue": name, "error": str(error)[:200]} for name in names]


def _reviews(hours: int = 24) -> dict[str, int]:
	rows = db.rows(
		"SELECT status, COUNT(*) FROM `tabCode Review` WHERE creation > %s GROUP BY status",
		(add_to_date(now_datetime(), hours=-hours),),
	)
	return {str(status): int(count) for status, count in rows}


def _failures(hours: int = 24) -> list[dict[str, Any]]:
	"""Failed reviews grouped by the start of their error."""
	rows = db.rows(
		"""SELECT LEFT(COALESCE(error, ''), 90) AS cause, COUNT(*) AS n FROM `tabCode Review`
		WHERE status = 'Failed' AND creation > %s GROUP BY cause ORDER BY n DESC LIMIT 10""",
		(add_to_date(now_datetime(), hours=-hours),),
	)
	return [{"cause": str(cause) or "(no error)", "count": int(n)} for cause, n in rows]


def _ai_providers() -> list[dict[str, Any]]:
	"""The breaker of each AI provider. The blank one only when it has failed: it serves no site
	that has AI providers."""
	states = [breaker.status(name) for name in breaker.known()]
	return [s for s in states if s["ai_provider"] or s["failures_in_a_row"] or s["outage_since"]]


def _indexes() -> list[dict[str, Any]]:
	"""Indexes that failed, run for too long, or that an enabled repository lacks."""
	stuck = add_to_date(now_datetime(), hours=-STUCK_AFTER)
	rows = db.dict_rows(
		f"""SELECT r.full_name, i.status, i.files_processed, i.num_files, i.started_at,
			LEFT(COALESCE(i.error, ''), 200) AS error
		FROM `tabRepository Index` i JOIN `tabCode Repository` r ON r.name = i.repository
		WHERE i.status = 'failed' OR (i.status IN ({", ".join(["%s"] * len(RUNNING))}) AND i.started_at < %s)
		ORDER BY r.full_name""",
		(*RUNNING, stuck),
	)
	missing = db.dict_rows(
		"""SELECT full_name, index_status AS status FROM `tabCode Repository`
		WHERE reviews_enabled = 1 AND (default_index IS NULL OR default_index = '')"""
	)
	return [*rows, *({**row, "error": "not indexed yet"} for row in missing)]
