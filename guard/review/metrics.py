"""How reviews ran over a period: throughput, time, turns, rounds, tokens and cost.

The `guard-health` page shows these numbers as cards, a table for each tier and charts.
"""

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import frappe
from frappe.utils import flt, now_datetime

from guard import settings as guard_settings
from guard.utils import db

PERIODS = (1, 7, 30)  # days that the page offers
ENDED = ("Completed", "Failed", "Cancelled")
# The stages in the order the pipeline runs them, for the chart of where the time goes.
STAGES = ("prepare", "start", "index", "context", "review", "verify", "filter", "publish", "after")


@dataclass
class Review:
	"""One review that ended in the period."""

	ended: datetime
	created: datetime
	started: datetime | None
	status: str
	tier: str
	seconds: float
	turns: int
	rounds: int
	input_tokens: int
	output_tokens: int
	cached_tokens: int
	findings: int
	posted: int
	attempt: int
	stages: dict[str, float] = field(default_factory=dict)

	@property
	def wait(self) -> float | None:
		"""Seconds from the request to the start of the last attempt."""
		return (self.started - self.created).total_seconds() if self.started else None

	@property
	def agent_seconds(self) -> float:
		"""Time of the review stage, where the agents run. Older reviews have no stage times."""
		return self.stages.get("review") or self.seconds


def metrics(days: int) -> dict[str, Any]:
	days = days if days in PERIODS else PERIODS[0]
	now = now_datetime()
	reviews = _reviews(now - timedelta(days=days))
	prices = guard_settings.prices()
	tiers = sorted({r.tier for r in reviews if r.tier})
	return {
		"days": days,
		"in_flight": _in_flight(),
		"summary": {
			**_stats(reviews, prices),
			"per_hour": round(sum(r.status == "Completed" for r in reviews) / (days * 24), 2),
		},
		"tiers": [{"tier": t, **_stats([r for r in reviews if r.tier == t], prices)} for t in tiers],
		"stages": _stages([r for r in reviews if r.status == "Completed"]),
		"series": _series(reviews, now, days, prices),
	}


def _reviews(since: datetime) -> list[Review]:
	rows = db.dict_rows(
		f"""SELECT completed_at, creation, started_at, status, tier, duration, agent_turns, agent_rounds,
			input_tokens, output_tokens, cached_tokens, findings_count, posted_count, attempt, stage_seconds
		FROM `tabCode Review`
		WHERE completed_at > %s AND status IN ({", ".join(["%s"] * len(ENDED))})""",
		(since, *ENDED),
	)
	return [
		Review(
			# MariaDB gives Datetime columns as `datetime`.
			ended=row["completed_at"],
			created=row["creation"],
			started=row["started_at"],
			status=str(row["status"]),
			tier=str(row["tier"] or ""),
			seconds=flt(row["duration"]),
			turns=int(row["agent_turns"] or 0),
			rounds=int(row["agent_rounds"] or 0),
			input_tokens=int(row["input_tokens"] or 0),
			output_tokens=int(row["output_tokens"] or 0),
			cached_tokens=int(row["cached_tokens"] or 0),
			findings=int(row["findings_count"] or 0),
			posted=int(row["posted_count"] or 0),
			attempt=int(row["attempt"] or 0),
			stages=_stage_seconds(row["stage_seconds"]),
		)
		for row in rows
	]


def _stage_seconds(value: Any) -> dict[str, float]:
	parsed = frappe.parse_json(value) if value else {}
	return {str(k): flt(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def _in_flight() -> dict[str, int]:
	rows = db.rows(
		"SELECT status, COUNT(*) FROM `tabCode Review` WHERE status IN ('Queued', 'Running') GROUP BY status"
	)
	counts = {str(status): int(n) for status, n in rows}
	return {"queued": counts.get("Queued", 0), "running": counts.get("Running", 0)}


def _stats(reviews: list[Review], prices: guard_settings.Prices) -> dict[str, Any]:
	"""The same numbers for the period, a tier or a bucket of time.

	Time, turns and findings come from the completed reviews. Tokens and cost come from every
	review, because a failed or cancelled review also spent them.
	"""
	done = [r for r in reviews if r.status == "Completed"]
	worked = [r for r in done if r.turns]
	minutes = sorted(r.seconds / 60 for r in done)
	waits = sorted(w / 60 for r in done if (w := r.wait) is not None)
	agent_minutes = sum(r.agent_seconds for r in worked) / 60
	input_tokens = sum(r.input_tokens for r in reviews)
	cached_tokens = sum(r.cached_tokens for r in reviews)
	cost = sum(prices.cost(r.input_tokens, r.cached_tokens, r.output_tokens) for r in reviews)
	return {
		"completed": len(done),
		"failed": sum(r.status == "Failed" for r in reviews),
		"cancelled": sum(r.status == "Cancelled" for r in reviews),
		"duration_avg": _avg(minutes),
		"duration_p50": _percentile(minutes, 50),
		"duration_p90": _percentile(minutes, 90),
		"wait_p50": _percentile(waits, 50),
		"wait_p90": _percentile(waits, 90),
		"turns": _avg([r.turns for r in worked]),
		# Reviews from before rounds were counted have none.
		"rounds": _avg([r.rounds for r in worked if r.rounds]),
		"turns_per_minute": round(sum(r.turns for r in worked) / agent_minutes, 2) if agent_minutes else None,
		"input_tokens": input_tokens,
		"output_tokens": sum(r.output_tokens for r in reviews),
		"cached_tokens": cached_tokens,
		"cache_rate": round(cached_tokens / input_tokens, 2) if input_tokens else None,
		"cost": round(cost, 2) if prices.known else None,
		"cost_per_review": round(cost / len(done), 3) if prices.known and done else None,
		"findings": _avg([r.findings for r in done]),
		"posted": _avg([r.posted for r in done]),
		"retried": round(sum(r.attempt > 0 for r in done) / len(done), 2) if done else None,
	}


def _stages(done: list[Review]) -> list[dict[str, Any]]:
	"""The mean seconds of each stage, over the completed reviews that have stage times."""
	timed = [r for r in done if r.stages]
	if not timed:
		return []
	return [
		{"stage": s, "seconds": round(sum(r.stages.get(s, 0) for r in timed) / len(timed), 1)} for s in STAGES
	]


def _series(
	reviews: list[Review], now: datetime, days: int, prices: guard_settings.Prices
) -> list[dict[str, Any]]:
	"""Stats for each hour of one day, or for each day of a longer period, oldest first."""
	hourly = days == 1
	step, label = (timedelta(hours=1), "%H:00") if hourly else (timedelta(days=1), "%d %b")
	count = 24 if hourly else days
	starts = [_floor(now, hourly) - step * i for i in reversed(range(count))]
	buckets: dict[datetime, list[Review]] = {start: [] for start in starts}
	for review in reviews:
		if (start := _floor(review.ended, hourly)) in buckets:
			buckets[start].append(review)
	return [{"label": start.strftime(label), **_stats(buckets[start], prices)} for start in starts]


def _floor(moment: datetime, hourly: bool) -> datetime:
	"""The start of the hour or the day of `moment`."""
	moment = moment.replace(minute=0, second=0, microsecond=0)
	return moment if hourly else moment.replace(hour=0)


def _avg(values: list[int] | list[float]) -> float | None:
	return round(sum(values) / len(values), 1) if values else None


def _percentile(ordered: list[float], percent: int) -> float | None:
	"""Nearest-rank percentile of a sorted list."""
	if not ordered:
		return None
	return round(ordered[max(math.ceil(percent / 100 * len(ordered)) - 1, 0)], 1)
