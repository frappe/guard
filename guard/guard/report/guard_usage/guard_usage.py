"""Guard Usage: tokens, cache share, estimated cost and credits of reviews, by day, tier or repository."""

from datetime import date, timedelta
from typing import Any

from frappe import _
from frappe.utils import add_days, getdate

from guard import settings as guard_settings
from guard.settings import TIERS, PriceList
from guard.utils import db

GROUPS = {
	"Day": "DATE(r.creation)",
	"Tier": "r.tier",
	"Repository": "repo.full_name",
}
# Summed over the reviews of each row.
COUNTS = (
	"reviews",
	"completed",
	"credits",
	"input_tokens",
	"cached_tokens",
	"output_tokens",
	"agent_turns",
)


def execute(filters: dict | None = None) -> tuple:
	filters = filters or {}
	group = str(filters.get("group_by") or "Day")
	if group not in GROUPS:
		group = "Day"
	prices = guard_settings.prices()
	rows = [with_totals(row) for row in usage(filters, group, prices)]
	message = (
		None
		if prices.known
		else _("Set the token prices in {0} to see the estimated cost.").format(
			'<a href="/app/guard-settings">Guard Settings</a>'
		)
	)
	return columns(group, prices), rows, message, chart(rows, group, prices), summary(rows, prices)


def usage(filters: dict, group: str, prices: PriceList) -> list[dict[str, Any]]:
	"""One row each group. Tokens count every attempt of every review. Credits count completed reviews,
	as the monthly credit cap does. The cost of each tier uses the prices of that tier."""
	conditions = ["r.creation >= %(start)s", "r.creation < %(end)s"]
	values: dict[str, Any] = {
		"start": day(filters.get("from_date"), -29),
		"end": add_days(day(filters.get("to_date"), 0), 1),
	}
	if filters.get("tier"):
		conditions.append("r.tier = %(tier)s")
		values["tier"] = filters["tier"]
	if filters.get("repository"):
		conditions.append("r.repository = %(repository)s")
		values["repository"] = filters["repository"]
	key = GROUPS[group]
	rows = db.dict_rows(
		# nosemgrep: frappe-sql-format-injection - the key and the conditions are constants of this module
		f"""SELECT {key} AS bucket, COUNT(*) AS reviews,
			SUM(r.status = 'Completed') AS completed,
			SUM(IF(r.status = 'Completed', r.credits, 0)) AS credits,
			SUM(r.input_tokens) AS input_tokens, SUM(r.cached_tokens) AS cached_tokens,
			SUM(r.output_tokens) AS output_tokens, SUM(r.agent_turns) AS agent_turns, r.tier AS tier
		FROM `tabCode Review` r
		LEFT JOIN `tabCode Repository` repo ON repo.name = r.repository
		WHERE {" AND ".join(conditions)}
		GROUP BY bucket, r.tier""",
		values,
	)
	groups: dict[Any, dict[str, Any]] = {}
	for row in rows:
		counts = {name: int(row[name] or 0) for name in COUNTS}
		cost = prices.cost(
			str(row["tier"] or ""), counts["input_tokens"], counts["cached_tokens"], counts["output_tokens"]
		)
		merged = groups.setdefault(
			row["bucket"], {"bucket": row["bucket"], "cost": 0.0, **dict.fromkeys(COUNTS, 0)}
		)
		for name in COUNTS:
			merged[name] += counts[name]
		merged["cost"] += cost
	return sorted(groups.values(), key=lambda row: order(row, group))


def day(value: Any, default_offset: int) -> date:
	"""The date of a filter, or today plus `default_offset` days when it is empty."""
	if value and (picked := getdate(value)):
		return picked
	# getdate() is today in the site's time zone.
	return (getdate() or date.today()) + timedelta(days=default_offset)


def order(row: dict[str, Any], group: str) -> Any:
	if group == "Day":
		return str(row["bucket"])
	if group == "Tier":
		return TIERS.index(row["bucket"]) if row["bucket"] in TIERS else len(TIERS)
	return -int(row["input_tokens"] or 0)


def with_totals(row: dict[str, Any]) -> dict[str, Any]:
	if not row["bucket"]:
		# A review stopped before the gate picked its tier, or its repository is gone.
		row["bucket"] = _("None")
	return {
		**row,
		"cache_share": share(row["cached_tokens"], row["input_tokens"]),
		"cost_per_review": row["cost"] / row["completed"] if row["completed"] else 0.0,
	}


def share(part: int, whole: int) -> float:
	return round(100 * part / whole, 1) if whole else 0.0


def compact(count: int) -> str:
	"""A token count short enough for a summary card: 2.73M, 41.6K."""
	for size, unit in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
		if count >= size:
			return f"{count / size:.3g}{unit}"
	return str(count)


def columns(group: str, prices: PriceList) -> list[dict[str, Any]]:
	bucket = {"fieldname": "bucket", "label": _(group), "width": 220}
	bucket["fieldtype"] = "Date" if group == "Day" else "Data"
	tokens = {"fieldtype": "Int"}
	result = [
		bucket,
		{"fieldname": "reviews", "label": _("Reviews"), "fieldtype": "Int", "width": 90},
		{"fieldname": "completed", "label": _("Completed"), "fieldtype": "Int", "width": 100},
		{"fieldname": "credits", "label": _("Credits"), "fieldtype": "Int", "width": 90},
		{"fieldname": "input_tokens", "label": _("Input Tokens"), **tokens, "width": 140},
		{"fieldname": "cached_tokens", "label": _("Cached Tokens"), **tokens, "width": 140},
		{"fieldname": "cache_share", "label": _("Cache Share"), "fieldtype": "Percent", "width": 110},
		{"fieldname": "output_tokens", "label": _("Output Tokens"), **tokens, "width": 130},
		{"fieldname": "agent_turns", "label": _("Agent Turns"), "fieldtype": "Int", "width": 110},
	]
	if prices.known:
		result += [
			{
				"fieldname": "cost",
				"label": _("Est. Cost (USD)"),
				"fieldtype": "Float",
				"precision": 2,
				"width": 130,
			},
			{
				"fieldname": "cost_per_review",
				"label": _("Per Completed Review (USD)"),
				"fieldtype": "Float",
				"precision": 3,
				"width": 190,
			},
		]
	return result


def chart(rows: list[dict[str, Any]], group: str, prices: PriceList) -> dict[str, Any]:
	"""Estimated cost when the prices are set. Without them: uncached and cached input tokens."""
	labels = [str(row["bucket"]) for row in rows]
	if prices.known:
		datasets = [{"name": _("Est. Cost (USD)"), "values": [round(row["cost"], 2) for row in rows]}]
	else:
		datasets = [
			{
				"name": _("Uncached Input"),
				"values": [row["input_tokens"] - row["cached_tokens"] for row in rows],
			},
			{"name": _("Cached Input"), "values": [row["cached_tokens"] for row in rows]},
		]
	return {
		"data": {"labels": labels, "datasets": datasets},
		"type": "line" if group == "Day" and prices.known else "bar",
		"barOptions": {"stacked": 1},
	}


def summary(rows: list[dict[str, Any]], prices: PriceList) -> list[dict[str, Any]]:
	total = {name: sum(row[name] for row in rows) for name in COUNTS}
	cards = [
		{"label": _("Reviews"), "value": total["reviews"], "datatype": "Int", "indicator": "Blue"},
		{"label": _("Credits"), "value": total["credits"], "datatype": "Int", "indicator": "Blue"},
		{
			"label": _("Input Tokens"),
			"value": compact(total["input_tokens"]),
			"datatype": "Data",
			"indicator": "Grey",
		},
		{
			"label": _("Cache Share"),
			"value": share(total["cached_tokens"], total["input_tokens"]),
			"datatype": "Percent",
			"indicator": "Green",
		},
		{
			"label": _("Output Tokens"),
			"value": compact(total["output_tokens"]),
			"datatype": "Data",
			"indicator": "Grey",
		},
	]
	if prices.known:
		# Five cards fit on one row: the cost takes the place of the output tokens.
		cost = sum(row["cost"] for row in rows)
		cards[-1] = {
			"label": _("Est. Cost (USD)"),
			"value": f"${cost:,.2f}",
			"datatype": "Data",
			"indicator": "Orange",
		}
	return cards
