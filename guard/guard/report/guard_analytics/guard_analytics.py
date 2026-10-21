"""Guard Analytics: summary cards, a chart of the chosen metric, and its leaderboard."""

from typing import Any

from frappe import _

from guard import analytics
from guard.utils import db

METRICS = {
	"reviews": "reviews",
	"bugs": "bugs",
	"merge_time": "reviews",
	"ratings": "upvoted",
	"contributors": "contributors",
}


def execute(filters: dict | None = None) -> tuple:
	filters = filters or {}
	scope = scope_from(filters)
	metric = filters.get("metric") or "reviews"
	board = analytics.leaderboard(scope, METRICS.get(metric, "reviews"))
	columns = [
		{"fieldname": "label", "label": _("Name"), "fieldtype": "Data", "width": 320},
		{"fieldname": "value", "label": _("Value"), "fieldtype": "Float", "width": 120},
	]
	if metric == "contributors":
		columns.append(
			{"fieldname": "repositories", "label": _("Repositories"), "fieldtype": "Data", "width": 300}
		)
	series = analytics.series(
		scope, metric if metric != "contributors" else "reviews", filters.get("bucket") or "day"
	)
	chart = {
		"data": {"labels": [r["bucket"] for r in series], "datasets": _datasets(metric, series)},
		"type": "bar" if metric in ("bugs", "ratings") else "line",
		"barOptions": {"stacked": 1},
	}
	return columns, board, None, chart, cards(scope)


def cards(scope: analytics.Filters) -> list[dict[str, Any]]:
	summary = analytics.summary(scope)
	return [
		{"label": _("Total PRs"), "value": summary["total_prs"], "indicator": "Blue"},
		{"label": _("Total Reviews"), "value": summary["total_reviews"], "indicator": "Blue"},
		{"label": _("Avg Merge Time (h)"), "value": summary["avg_merge_hours"] or 0, "indicator": "Grey"},
		{
			"label": _("Bugs Caught"),
			"value": summary["bugs_caught"],
			"indicator": "Red",
			"description": _("in {0} repositories").format(summary["bug_repositories"]),
		},
		{"label": _("Addressed Rate %"), "value": summary["addressed_rate"] or 0, "indicator": "Green"},
		{
			"label": _("Upvotes / Downvotes"),
			"value": f"{summary['upvotes']} / {summary['downvotes']}",
			"datatype": "Data",
			"indicator": "Grey",
		},
	]


def scope_from(filters: dict) -> analytics.Filters:
	repositories = [filters["repository"]] if filters.get("repository") else []
	teams = [filters["team"]] if filters.get("team") else []
	authors = [filters["author"]] if filters.get("author") else []
	scope = analytics.Filters.from_period(
		filters.get("period") or "this month", repositories=repositories, teams=teams, authors=authors
	)
	if filters.get("period") == "custom" and filters.get("from_date"):
		scope.start = db.scalar("SELECT TIMESTAMP(%s)", (filters["from_date"],))
		scope.end = db.scalar(
			"SELECT TIMESTAMP(%s) + INTERVAL 1 DAY", (filters.get("to_date") or filters["from_date"],)
		)
	return scope


def _datasets(metric: str, series: list[dict[str, Any]]) -> list[dict[str, Any]]:
	if metric == "bugs":
		return [{"name": s, "values": [r[s.lower()] or 0 for r in series]} for s in ("P0", "P1", "P2")]
	if metric == "ratings":
		return [
			{"name": _("Upvotes"), "values": [r["up"] or 0 for r in series]},
			{"name": _("Downvotes"), "values": [r["down"] or 0 for r in series]},
		]
	return [{"name": metric, "values": [float(r["value"] or 0) for r in series]}]
