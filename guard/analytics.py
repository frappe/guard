"""Review analytics. See docs/spec/11-surfaces.md (Analytics)."""

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from statistics import median
from typing import Any

from frappe.utils import getdate, now_datetime, nowdate

from guard.utils import db


@dataclass
class Filters:
	start: datetime | None = None
	end: datetime | None = None
	repositories: list[str] = field(default_factory=list)  # Code Repository names
	teams: list[str] = field(default_factory=list)
	authors: list[str] = field(default_factory=list)

	@classmethod
	def from_period(cls, period: str = "this month", **kwargs: Any) -> "Filters":
		return cls(start=period_start(period), end=None, **kwargs)

	def where(self, alias: str, date_field: str) -> tuple[str, list[Any]]:
		clauses, values = ["1=1"], []
		if self.start:
			clauses.append(f"{alias}.{date_field} >= %s")
			values.append(self.start)
		if self.end:
			clauses.append(f"{alias}.{date_field} < %s")
			values.append(self.end)
		if self.repositories:
			clauses.append(f"{alias}.repository IN %s")
			values.append(tuple(self.repositories))
		if self.teams:
			clauses.append(f"{alias}.repository IN (SELECT name FROM `tabCode Repository` WHERE team IN %s)")
			values.append(tuple(self.teams))
		return " AND ".join(clauses), values


def period_start(period: str) -> datetime | None:
	today = getdate(nowdate())
	if not isinstance(today, date):
		return None
	if period == "this week":
		start = today - timedelta(days=today.weekday())
	elif period == "this month":
		start = today.replace(day=1)
	elif period == "this quarter":
		start = today.replace(month=(today.month - 1) // 3 * 3 + 1, day=1)
	elif period == "this year":
		start = today.replace(month=1, day=1)
	else:
		return None
	return datetime.combine(start, time.min)


def summary(filters: Filters) -> dict[str, Any]:
	pr_where, pr_values = filters.where("p", "merged_at")
	author = _author(filters, "p.author")
	merged = db.dict_rows(
		f"""SELECT p.name, p.opened_at, p.merged_at FROM `tabPull Request` p
		WHERE p.state = 'merged' AND p.review_count > 0 AND {pr_where}{author[0]}""",
		(*pr_values, *author[1]),
	)
	hours = [
		(r["merged_at"] - r["opened_at"]).total_seconds() / 3600
		for r in merged
		if r["merged_at"] and r["opened_at"]
	]
	review_where, review_values = filters.where("r", "creation")
	reviews = db.scalar(
		f"SELECT COUNT(*) FROM `tabCode Review` r WHERE r.status = 'Completed' AND {review_where}",
		review_values,
	)
	bugs = bugs_caught(filters)
	return {
		"total_prs": len(merged),
		"total_reviews": int(reviews or 0),
		"avg_merge_hours": round(sum(hours) / len(hours), 1) if hours else None,
		"median_merge_hours": round(median(hours), 1) if hours else None,
		"bugs_caught": bugs["count"],
		"bug_repositories": bugs["repositories"],
		"addressed_rate": addressed_rate(filters),
		"upvotes": _votes(filters, "Upvote"),
		"downvotes": _votes(filters, "Downvote"),
	}


def bugs_caught(filters: Filters) -> dict[str, int]:
	where, values = filters.where("f", "creation")
	author = _author(filters, "f.author")
	row = db.rows(
		f"SELECT COUNT(*), COUNT(DISTINCT f.repository) FROM `tabReview Finding` f WHERE f.posted = 1 AND {where}{author[0]}",
		(*values, *author[1]),
	)[0]
	return {"count": int(row[0] or 0), "repositories": int(row[1] or 0)}


def addressed_rate(filters: Filters) -> float | None:
	where, values = filters.where("f", "creation")
	row = db.rows(
		f"""SELECT COUNT(*), SUM(f.status = 'Addressed' OR f.upvotes > f.downvotes)
		FROM `tabReview Finding` f JOIN `tabPull Request` p ON p.name = f.pull_request
		WHERE f.posted = 1 AND p.state IN ('closed', 'merged') AND {where}""",
		values,
	)[0]
	total = int(row[0] or 0)
	return round(100 * int(row[1] or 0) / total, 1) if total else None


def series(filters: Filters, metric: str = "reviews", bucket: str = "day") -> list[dict[str, Any]]:
	# Doubled percent signs: the query also takes parameters.
	fmt = {"day": "%%Y-%%m-%%d", "week": "%%x-W%%v", "month": "%%Y-%%m"}[bucket]
	if metric == "reviews":
		where, values = filters.where("r", "creation")
		query = f"SELECT DATE_FORMAT(r.creation, '{fmt}') AS bucket, COUNT(*) AS value FROM `tabCode Review` r WHERE r.status = 'Completed' AND {where} GROUP BY bucket ORDER BY bucket"
	elif metric == "bugs":
		where, values = filters.where("f", "creation")
		query = f"""SELECT DATE_FORMAT(f.creation, '{fmt}') AS bucket, SUM(f.severity = 'P0') AS p0, SUM(f.severity = 'P1') AS p1,
			SUM(f.severity = 'P2') AS p2, COUNT(*) AS value FROM `tabReview Finding` f WHERE f.posted = 1 AND {where} GROUP BY bucket ORDER BY bucket"""
	elif metric == "merge_time":
		where, values = filters.where("p", "merged_at")
		query = f"""SELECT DATE_FORMAT(p.merged_at, '{fmt}') AS bucket, AVG(TIMESTAMPDIFF(MINUTE, p.opened_at, p.merged_at)) / 60 AS value
			FROM `tabPull Request` p WHERE p.state = 'merged' AND {where} GROUP BY bucket ORDER BY bucket"""
	elif metric == "ratings":
		where, values = filters.where("b", "creation")
		query = f"""SELECT DATE_FORMAT(b.creation, '{fmt}') AS bucket, SUM(b.kind = 'Upvote') AS up, SUM(b.kind = 'Downvote') AS down, COUNT(*) AS value
			FROM `tabReview Feedback` b WHERE b.kind IN ('Upvote', 'Downvote') AND {where} GROUP BY bucket ORDER BY bucket"""
	else:
		raise ValueError(metric)
	return db.dict_rows(query, values)


def leaderboard(filters: Filters, metric: str = "reviews", limit: int = 10) -> list[dict[str, Any]]:
	if metric == "reviews":
		where, values = filters.where("r", "creation")
		query = f"""SELECT c.full_name AS label, COUNT(*) AS value FROM `tabCode Review` r JOIN `tabCode Repository` c ON c.name = r.repository
			WHERE r.status = 'Completed' AND {where} GROUP BY c.full_name ORDER BY value DESC LIMIT {int(limit)}"""
	elif metric == "bugs":
		where, values = filters.where("f", "creation")
		query = f"""SELECT c.full_name AS label, COUNT(*) AS value FROM `tabReview Finding` f JOIN `tabCode Repository` c ON c.name = f.repository
			WHERE f.posted = 1 AND {where} GROUP BY c.full_name ORDER BY value DESC LIMIT {int(limit)}"""
	elif metric == "contributors":
		where, values = filters.where("p", "merged_at")
		query = f"""SELECT p.author AS label, COUNT(*) AS value, GROUP_CONCAT(DISTINCT c.full_name) AS repositories FROM `tabPull Request` p
			JOIN `tabCode Repository` c ON c.name = p.repository WHERE p.state = 'merged' AND {where} GROUP BY p.author ORDER BY value DESC LIMIT {int(limit)}"""
	elif metric == "upvoted":
		where, values = filters.where("f", "creation")
		query = f"SELECT f.title AS label, f.upvotes AS value, f.name FROM `tabReview Finding` f WHERE f.upvotes > 0 AND {where} ORDER BY f.upvotes DESC LIMIT {int(limit)}"
	else:
		raise ValueError(metric)
	return db.dict_rows(query, values)


def findings(
	filters: Filters,
	severities: list[str] | None = None,
	statuses: list[str] | None = None,
	security: bool | None = None,
	search: str = "",
	limit: int = 100,
	offset: int = 0,
) -> list[dict[str, Any]]:
	where, values = filters.where("f", "creation")
	author = _author(filters, "f.author")
	clauses = [f"f.posted = 1 AND {where}{author[0]}"]
	values += author[1]
	if severities:
		clauses.append("f.severity IN %s")
		values.append(tuple(severities))
	if statuses:
		clauses.append("(CASE WHEN f.status = 'Addressed' THEN 'addressed' ELSE 'open' END) IN %s")
		values.append(tuple(s.lower() for s in statuses))
	if security is not None:
		clauses.append("f.is_security = %s")
		values.append(int(security))
	if search:
		clauses.append("(f.title LIKE %s OR f.path LIKE %s OR p.title LIKE %s OR c.full_name LIKE %s)")
		values += [f"%{search}%"] * 4
	return db.dict_rows(
		f"""SELECT f.name, f.title AS finding, f.severity, f.is_security, c.full_name AS repository, p.number, p.title AS pull_request_title,
			p.web_url, f.comment_url, f.path, f.status, f.creation AS date, f.author
		FROM `tabReview Finding` f LEFT JOIN `tabPull Request` p ON p.name = f.pull_request
		LEFT JOIN `tabCode Repository` c ON c.name = f.repository
		WHERE {" AND ".join(clauses)} ORDER BY f.creation DESC LIMIT {int(limit)} OFFSET {int(offset)}""",
		values,
	)


def mine(login: str, filters: Filters) -> dict[str, Any]:
	"""Stats for one developer's pull requests (the My Guard report)."""
	return summary(Filters(filters.start, filters.end, filters.repositories, filters.teams, [login]))


def _votes(filters: Filters, kind: str) -> int:
	where, values = filters.where("b", "creation")
	return int(
		db.scalar(
			f"SELECT COUNT(*) FROM `tabReview Feedback` b WHERE b.kind = %s AND {where}", (kind, *values)
		)
		or 0
	)


def _author(filters: Filters, column: str) -> tuple[str, list[Any]]:
	if not filters.authors:
		return "", []
	return f" AND {column} IN %s", [tuple(filters.authors)]


def now() -> datetime:
	return now_datetime()
