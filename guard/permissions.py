"""Team-scoped permissions, and the repositories of guests. See docs/spec/12-security.md."""

from typing import Any

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions

from guard.utils import db

# Frappe treats any falsy result of a has_permission hook as a denial. Hooks only deny,
# so they return True when they have no objection, never None.
ADMIN_ROLES = {"Guard Admin", "System Manager", "Administrator"}
# The roles that can put a review on a pull request.
REVIEWER_ROLES = ("Guard Admin", "Guard Member", "System Manager")
WRITE_TYPES = {"write", "create", "delete", "submit", "cancel", "amend"}
GUEST_ROLE = "Guard Guest"
# A user with one of these roles is not a guest, also when they have the Guard Guest role.
TEAM_ROLES = ADMIN_ROLES | {"Guard Member", "Guard Viewer"}
# The code graph tables are large and have no index on `repository`. Their `index` column is indexed.
GRAPH_DOCTYPES = {"Code Entity", "Code Edge", "Code Chunk"}


def is_admin(user: str | None = None) -> bool:
	return bool(ADMIN_ROLES & set(frappe.get_roles(user or frappe.session.user)))


def is_guest(user: str | None = None) -> bool:
	"""An outside collaborator: the Guard Guest role and no other Guard role."""
	roles = set(frappe.get_roles(user or frappe.session.user))
	return GUEST_ROLE in roles and not roles & TEAM_ROLES


def guest_repositories(user: str | None = None) -> list[str]:
	"""The repositories that a User Permission gives to the user. A guest sees only these. Frappe applies
	a User Permission with `applicable_for` only to that DocType, so it gives no repository."""
	granted = get_user_permissions(user or frappe.session.user).get("Code Repository") or []
	return [
		str(permission.doc)
		for permission in granted
		if permission.get("applicable_for") in (None, "", "Code Repository")
	]


def repository_query(user: str | None = None, doctype: str | None = None) -> str:
	"""Guests list only the rows of the repositories that they have, and nothing without a User
	Permission: Frappe applies no User Permission when a user has none."""
	if not is_guest(user):
		return ""
	names = guest_repositories(user)
	if not names:
		return "1=0"
	values = ", ".join(frappe.db.escape(name) for name in names)
	if doctype == "Code Repository":
		return f"`tabCode Repository`.`name` in ({values})"
	if doctype in GRAPH_DOCTYPES:
		return f"`tab{doctype}`.`index` in (select `name` from `tabRepository Index` where `repository` in ({values}))"
	return f"`tab{doctype}`.`repository` in ({values})"


def user_query(user: str | None = None) -> str:
	"""Every desk user can search the other users in a Link field. A guest finds only themselves."""
	user = user or frappe.session.user
	return f"`tabUser`.`name` = {frappe.db.escape(user)}" if is_guest(user) else ""


@frappe.whitelist()
def names_for_mentions(search_term: str, page_length: int = 10) -> list[dict[str, Any]]:
	"""The @mention search of Frappe lists every desk user. A guest gets nobody."""
	from frappe.desk.search import get_names_for_mentions

	return [] if is_guest() else get_names_for_mentions(search_term, page_length)


def repository_content(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	"""Guests read the indexes, the graph and the knowledge base of their repositories only."""
	return not is_guest(user) or str(doc.get("repository") or "") in guest_repositories(user)


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


def custom_context(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	"""Organization rules: admins only. Team rules: admins and that team's admins. Reads are open."""
	if ptype not in WRITE_TYPES or is_admin(user):
		return True
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
	return True


def review_settings(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	"""The organization record: admins only. A team record: admins and that team's admins."""
	if ptype not in WRITE_TYPES or is_admin(user):
		return True
	scope = doc.get("scope") if not isinstance(doc, str) else db.value("Review Settings", doc, "scope")
	team = doc.get("team") if not isinstance(doc, str) else db.value("Review Settings", doc, "team")
	return scope == "Team" and is_team_admin(team, user)


def code_repository(doc: Any, ptype: str | None = None, user: str | None = None) -> bool:
	"""Members enable repositories of their own teams. Guests read only the repositories that they have."""
	name = doc if isinstance(doc, str) else str(doc.get("name") or "")
	if is_guest(user):
		return ptype not in WRITE_TYPES and name in guest_repositories(user)
	if ptype not in WRITE_TYPES or is_admin(user):
		return True
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
	return bool(member)
