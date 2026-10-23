"""The state of Guard in one view: queues, workers, reviews, the AI gateway and the indexes.

The `guard-health` page shows `status()`. `check_failure_rate()` runs every 15 minutes and
notifies admins when many reviews fail.
"""

from typing import Any

import frappe
from frappe.utils import add_to_date, now_datetime

from guard.indexing.indexer import RUNNING
from guard.llm import breaker
from guard.utils import cache, db, notify

QUEUES = ("short", "default", "long")
STUCK_AFTER = 2  # hours an index may run before it counts as stuck
ALERT_RATE = 0.2  # failed share of the reviews in the last hour that notifies admins
ALERT_MIN = 5  # reviews in the hour before the rate counts


@frappe.whitelist()
def status() -> dict[str, Any]:
	frappe.only_for(["Guard Admin", "System Manager"])
	return {
		"queues": _queues(),
		"reviews": _reviews(),
		"failures": _failures(),
		"gateway": _gateway(),
		"indexes": _indexes(),
		"failure_rate": failure_rate(),
	}


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


def _queues() -> list[dict[str, Any]]:
	from frappe.utils.background_jobs import get_queue, get_workers
	from rq.registry import StartedJobRegistry

	try:
		workers = get_workers()
		rows = []
		for name in QUEUES:
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
		return [{"queue": name, "error": str(error)[:200]} for name in QUEUES]


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


def _gateway() -> dict[str, Any]:
	outage = cache.get(breaker.OUTAGE)
	return {
		"paused_for": breaker.wait_seconds(),
		"failures_in_a_row": int(cache.get(breaker.FAILURES) or 0),
		"outage_since": outage.get("since") if isinstance(outage, dict) else None,
		"last_error": outage.get("reason") if isinstance(outage, dict) else None,
	}


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
