"""My Guard: reviews and findings on the pull requests of the current user."""

import frappe
from frappe import _

from guard import analytics
from guard.guard.report.guard_analytics.guard_analytics import cards, scope_from
from guard.utils import db


def execute(filters: dict | None = None) -> tuple:
	filters = filters or {}
	login = db.value("Guard User", {"user": frappe.session.user}, "github_login")
	if not login:
		return [{"fieldname": "note", "label": _("Note"), "fieldtype": "Data", "width": 600}], [
			{"note": _("Add your GitHub login to your Guard User record to see your stats.")}
		]
	scope = scope_from({**filters, "author": login})
	rows = db.dict_rows(
		"""SELECT p.name, p.number, p.title, c.full_name AS repository, p.state, p.review_count, p.confidence_score,
			(SELECT COUNT(*) FROM `tabReview Finding` f WHERE f.pull_request = p.name AND f.posted = 1) AS findings,
			(SELECT COUNT(*) FROM `tabReview Finding` f WHERE f.pull_request = p.name AND f.posted = 1 AND f.status = 'Open') AS open_findings,
			p.opened_at
		FROM `tabPull Request` p JOIN `tabCode Repository` c ON c.name = p.repository
		WHERE p.author = %s AND (%s IS NULL OR p.opened_at >= %s) ORDER BY p.opened_at DESC LIMIT 500""",
		(login, scope.start, scope.start),
	)
	columns = [
		{
			"fieldname": "name",
			"label": _("Pull Request"),
			"fieldtype": "Link",
			"options": "Pull Request",
			"width": 110,
		},
		{"fieldname": "title", "label": _("Title"), "fieldtype": "Data", "width": 320},
		{"fieldname": "repository", "label": _("Repository"), "fieldtype": "Data", "width": 180},
		{"fieldname": "state", "label": _("State"), "fieldtype": "Data", "width": 80},
		{"fieldname": "review_count", "label": _("Reviews"), "fieldtype": "Int", "width": 80},
		{"fieldname": "confidence_score", "label": _("Score"), "fieldtype": "Int", "width": 70},
		{"fieldname": "findings", "label": _("Findings"), "fieldtype": "Int", "width": 80},
		{"fieldname": "open_findings", "label": _("Open"), "fieldtype": "Int", "width": 70},
		{"fieldname": "opened_at", "label": _("Opened"), "fieldtype": "Datetime", "width": 150},
	]
	return columns, rows, None, None, cards(analytics.Filters(scope.start, scope.end, authors=[login]))
