"""Custom number cards for the Guard workspace."""

from typing import Any

import frappe

from guard import analytics
from guard.utils import db


@frappe.whitelist()
def addressed_rate() -> dict[str, Any]:
	rate = analytics.addressed_rate(analytics.Filters.from_period("this quarter"))
	return {"value": rate or 0, "fieldtype": "Percent", "route": ["query-report", "Guard Analytics"]}


@frappe.whitelist()
def my_open_findings() -> dict[str, Any]:
	login = db.value("Guard User", {"user": frappe.session.user}, "github_login")
	count = (
		db.scalar(
			"SELECT COUNT(*) FROM `tabReview Finding` WHERE author = %s AND status = 'Open' AND posted = 1",
			(login,),
		)
		if login
		else 0
	)
	return {"value": int(count or 0), "fieldtype": "Int", "route": ["query-report", "My Guard"]}
