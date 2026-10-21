"""Bugs Caught: one row for each posted finding, with severity cards."""

from frappe import _

from guard import analytics
from guard.guard.report.guard_analytics.guard_analytics import scope_from


def execute(filters: dict | None = None) -> tuple:
	filters = filters or {}
	scope = scope_from({**filters, "period": filters.get("period") or "all time"})
	security = {"Yes": True, "No": False}.get(filters.get("security") or "")
	rows = analytics.findings(
		scope,
		severities=[filters["severity"]] if filters.get("severity") else None,
		statuses=[filters["status"]] if filters.get("status") else None,
		security=security,
		search=filters.get("search") or "",
		limit=1000,
	)
	for row in rows:
		row["security"] = "🔒" if row.get("is_security") else ""
		row["status"] = "Addressed" if row["status"] == "Addressed" else "Open"
	columns = [
		{
			"fieldname": "name",
			"label": _("Finding"),
			"fieldtype": "Link",
			"options": "Review Finding",
			"width": 110,
		},
		{"fieldname": "finding", "label": _("Title"), "fieldtype": "Data", "width": 340},
		{"fieldname": "severity", "label": _("Severity"), "fieldtype": "Data", "width": 80},
		{"fieldname": "security", "label": _("Security"), "fieldtype": "Data", "width": 70},
		{"fieldname": "repository", "label": _("Repository"), "fieldtype": "Data", "width": 180},
		{"fieldname": "number", "label": _("PR"), "fieldtype": "Int", "width": 70},
		{"fieldname": "path", "label": _("File"), "fieldtype": "Data", "width": 220},
		{"fieldname": "status", "label": _("Status"), "fieldtype": "Data", "width": 90},
		{"fieldname": "author", "label": _("Author"), "fieldtype": "Data", "width": 120},
		{"fieldname": "date", "label": _("Date"), "fieldtype": "Datetime", "width": 150},
	]
	cards = [
		{"label": _("All bugs"), "value": len(rows), "indicator": "Blue"},
		{"label": _("Security"), "value": sum(1 for r in rows if r["security"]), "indicator": "Red"},
		*(
			{"label": s, "value": sum(1 for r in rows if r["severity"] == s), "indicator": c}
			for s, c in (("P0", "Red"), ("P1", "Orange"), ("P2", "Yellow"))
		),
	]
	return columns, rows, None, None, cards
