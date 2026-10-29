"""A circuit breaker for each AI provider.

When an AI provider keeps failing, reviews that need it wait without using up their retries, admins
hear about it once, and the reviews that failed during the outage run again when it answers. An
outage of one AI provider does not pause the work that uses another one.

The blank name is the AI provider of calls without one: offline calls, and calls in tests.
"""

import time
from collections.abc import Iterable
from typing import Any

import frappe
from frappe.utils import now_datetime

from guard.review.jobs import retry_review
from guard.utils import cache, db, notify

THRESHOLD = 3  # outages in a row that open the breaker
PROBE_AFTER = 600  # seconds the breaker stays open before a review tries the AI provider again
FAILURES = "guard:gateway:failures"
OPEN = "guard:gateway:open"
OUTAGE = "guard:gateway:outage"
FAILED_PREFIX = "AI gateway down"  # the start of the error of a review that failed in an outage


def wait_seconds(ai_providers: Iterable[str] | None = None) -> int:
	"""Seconds until work may try these AI providers again: the longest wait of them. None means all.

	0 when all their breakers are closed."""
	names = known() if ai_providers is None else ai_providers
	return max((_wait(name) for name in set(names)), default=0)


def record_failure(reason: str, ai_provider: str = "") -> None:
	"""One more outage of an AI provider. The third in a row opens its breaker."""
	failures = int(cache.get(_key(FAILURES, ai_provider)) or 0) + 1
	cache.set(_key(FAILURES, ai_provider), failures, 3600)
	if failures < THRESHOLD:
		return
	cache.set(_key(OPEN, ai_provider), time.time() + PROBE_AFTER, PROBE_AFTER)
	if cache.get(_key(OUTAGE, ai_provider)):
		return
	cache.set(_key(OUTAGE, ai_provider), {"since": str(now_datetime()), "reason": reason[:500]}, 7 * 86400)
	_notify_admins(reason, ai_provider)


def record_success(ai_provider: str = "") -> None:
	"""The AI provider answered. Close its breaker, and run the reviews its outage failed."""
	if cache.get(_key(FAILURES, ai_provider)) is None and cache.get(_key(OUTAGE, ai_provider)) is None:
		return
	cache.delete(_key(FAILURES, ai_provider))
	cache.delete(_key(OPEN, ai_provider))
	outage = cache.get(_key(OUTAGE, ai_provider))
	if not isinstance(outage, dict):
		return
	cache.delete(_key(OUTAGE, ai_provider))
	rerun_failed(str(outage.get("since") or ""), ai_provider)


def status(ai_provider: str = "") -> dict[str, Any]:
	"""The state of the breaker of an AI provider, for Guard Health."""
	outage = cache.get(_key(OUTAGE, ai_provider))
	return {
		"ai_provider": ai_provider,
		"paused_for": _wait(ai_provider),
		"failures_in_a_row": int(cache.get(_key(FAILURES, ai_provider)) or 0),
		"outage_since": outage.get("since") if isinstance(outage, dict) else None,
		"last_error": outage.get("reason") if isinstance(outage, dict) else None,
	}


def known() -> list[str]:
	"""Every AI provider that can have a breaker: the blank one, then each AI Provider."""
	return ["", *frappe.get_all("AI Provider", pluck="name", order_by="name")]


def reset() -> None:
	"""Close every breaker and forget every outage. Tests use this: the cache outlives a test."""
	for name in known():
		for prefix in (FAILURES, OPEN, OUTAGE):
			cache.delete(_key(prefix, name))


def rerun_failed(since: str, ai_provider: str = "") -> list[str]:
	"""Queue again the reviews that failed on an AI provider since `since`.

	The error of such a review starts with FAILED_PREFIX, then the AI provider in brackets when it
	has a name (`review/pipeline.py`)."""
	where = f" ({ai_provider})" if ai_provider else ""
	names = [
		str(row[0])
		for row in db.rows(
			"SELECT name FROM `tabCode Review` WHERE status = 'Failed' AND error LIKE %s AND completed_at >= %s",
			(f"{FAILED_PREFIX}{_like(where)}:%", since),
		)
	]
	for name in names:
		retry_review(name)
	return names


def _wait(ai_provider: str) -> int:
	until = cache.get(_key(OPEN, ai_provider))
	if not until:
		return 0
	return max(1, int(float(until) - time.time()))


def _like(text: str) -> str:
	return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _key(prefix: str, ai_provider: str) -> str:
	# The blank AI provider keeps the keys from before there were several.
	return f"{prefix}:{ai_provider}" if ai_provider else prefix


def _notify_admins(reason: str, ai_provider: str) -> None:
	what = f"the AI provider {ai_provider}" if ai_provider else "the AI gateway"
	notify.admins(
		f"Guard paused reviews: {what} is down",
		f"Reviews that need {what} wait until it answers again, then the ones that failed run again.\n\n"
		f"Last error: {reason[:500]}",
	)
