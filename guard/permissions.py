"""Team-scoped permissions. See docs/spec/12-security.md."""

from typing import Any

import frappe

from guard.utils import db

ADMIN_ROLES = {"Guard Admin", "System Manager", "Administrator"}
WRITE_TYPES = {"write", "create", "delete", "submit", "cancel", "amend"}


def is_admin(user: str | None = None) -> bool:
	return bool(ADMIN_ROLES & set(frappe.get_roles(user or frappe.session.user)))


def is_team_admin(team: str | None, user: str | None = None) -> bool:
	if not team:
		return False
	return bool(
		db.value(
			"Guard Team Member",
			{
				"parent": team,
				"parenttype": "Guard Team",
				"user": user or frappe.session.user,
				"role": "Admin",
			},
			"name",
		)
	)


def custom_context(doc: Any, ptype: str | None = None, user: str | None = None) -> bool | None:
	"""Organization rules: admins only. Team rules: admins and that team's admins. Reads are open."""
	if ptype not in WRITE_TYPES or is_admin(user):
		return None
	team = doc.get("team") if not isinstance(doc, str) else db.value("Custom Context", doc, "team")
	if not team or not is_team_admin(team, user):
		return False
	if (
		ptype == "write"
		and not isinstance(doc, str)
		and doc.get("status") == "Active"
		and doc.get("source") == "Suggested"
	):
		return is_team_admin(team, user)
	return None


def review_settings(doc: Any, ptype: str | None = None, user: str | None = None) -> bool | None:
	"""The organization record: admins only. A team record: admins and that team's admins."""
	if ptype not in WRITE_TYPES or is_admin(user):
		return None
	scope = doc.get("scope") if not isinstance(doc, str) else db.value("Review Settings", doc, "scope")
	team = doc.get("team") if not isinstance(doc, str) else db.value("Review Settings", doc, "team")
	return None if scope == "Team" and is_team_admin(team, user) else False


def code_repository(doc: Any, ptype: str | None = None, user: str | None = None) -> bool | None:
	"""Members enable repositories of their own teams."""
	if ptype not in WRITE_TYPES or is_admin(user):
		return None
	team = doc.get("team") if not isinstance(doc, str) else db.value("Code Repository", doc, "team")
	member = (
		db.value(
			"Guard Team Member",
			{"parent": team, "parenttype": "Guard Team", "user": user or frappe.session.user},
			"name",
		)
		if team
		else None
	)
	return None if member else False
