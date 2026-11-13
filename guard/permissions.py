"""Team-scoped permissions, and the repositories of guests. See docs/spec/12-security.md."""

from typing import Any

import frappe
from frappe.core.doctype.user_permission.user_permission import get_user_permissions
from frappe.share import get_shared

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
# The parts of the docinfo of a form that name no other user. The timeline, the shares, the views and
# the assignments name them, so a guest gets only these.
GUEST_DOCINFO = {
	"doctype",
	"name",
	"attachments",
	"permissions",
	"is_document_followed",
	"tags",
	"document_email",
}
# The fields of a document that list users: assignees, likes, viewers, the last comments.
GUEST_HIDDEN_FIELDS = ("_assign", "_liked_by", "_seen", "_comments")
SHARE_FIELDS = ("name", "user", "everyone", "read", "write", "submit", "share", "notify")


def is_admin(user: str | None = None) -> bool:
	return bool(ADMIN_ROLES & set(frappe.get_roles(user or frappe.session.user)))


def is_guest(user: str | None = None) -> bool:
	"""An outside collaborator: the Guard Guest role and no other Guard role."""
	roles = set(frappe.get_roles(user or frappe.session.user))
	return GUEST_ROLE in roles and not roles & TEAM_ROLES


def guest_repositories(user: str | None = None) -> list[str]:
	"""The repositories that a share or a User Permission gives to the user. A guest sees only these.
	Frappe lists a shared document also when the query conditions deny it, so a share must also give
	the graph. Frappe applies a User Permission with `applicable_for` only to that DocType, so it gives
	no repository."""
	user = user or frappe.session.user
	granted = get_user_permissions(user).get("Code Repository") or []
	names = [
		str(permission.doc)
		for permission in granted
		if permission.get("applicable_for") in (None, "", "Code Repository")
	]
	return list(dict.fromkeys(names + [str(name) for name in get_shared("Code Repository", user)]))


def repository_query(user: str | None = None, doctype: str | None = None) -> str:
	"""Guests list only the rows of the repositories that they have, and nothing without a share or a
	User Permission: Frappe applies no User Permission when a user has none."""
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


@frappe.whitelist()
def getdoc(doctype: str, name: str | int) -> list | None:
	"""Open a form. A guest gets no other users in its docinfo."""
	from frappe.desk.form import load

	result = load.getdoc(doctype, name)
	_without_people()
	return result


@frappe.whitelist()
def get_docinfo(
	doc: dict | str | None = None, doctype: str | None = None, name: str | int | None = None
) -> None:
	"""Reload the sidebar and the timeline of a form. A guest gets no other users."""
	from frappe.desk.form import load

	load.get_docinfo(doc, doctype, name)
	_without_people()


# Save, submit, cancel and discard send the docinfo back with the document.
@frappe.whitelist(methods=["POST", "PUT"])
def savedocs(doc: str | dict, action: str) -> None:
	from frappe.desk.form import save

	save.savedocs(doc, action)
	_without_people()


@frappe.whitelist(methods=["POST", "PUT"])
def cancel(
	doctype: str | None = None,
	name: str | int | None = None,
	workflow_state_fieldname: str | None = None,
	workflow_state: str | None = None,
) -> None:
	from frappe.desk.form import save

	save.cancel(doctype, name, workflow_state_fieldname, workflow_state)
	_without_people()


@frappe.whitelist(methods=["POST", "PUT"])
def discard(doctype: str, name: str | int) -> None:
	from frappe.desk.form import save

	save.discard(doctype, name)
	_without_people()


@frappe.whitelist()
def shared_with(doctype: str, name: str) -> list:
	"""The users that a document is shared with. A guest gets only their own share."""
	from frappe.share import get_users

	shares = get_users(doctype, name)
	return [_own_share(s) for s in shares if s.get("user") == frappe.session.user] if is_guest() else shares


@frappe.whitelist()
def communications(doctype: str, name: str | int, start: str | int = 0, limit: str | int = 20) -> list:
	"""The emails of a document name their senders and recipients. A guest gets none."""
	from frappe.desk.form import load

	return [] if is_guest() else list(load.get_communications(doctype, name, start, limit) or [])


@frappe.whitelist()
def user_info_for_viewers(users: str | list) -> dict:
	"""The names of the users who look at the same form. A guest gets only themselves."""
	from frappe.desk.form import load

	if is_guest():
		users = [user for user in frappe.parse_json(users) if user == frappe.session.user]
	return load.get_user_info_for_viewers(users)


def _without_people() -> None:
	"""For a guest, drop the other users from the docinfo and the documents of the response."""
	if not is_guest():
		return
	for doc in frappe.response.get("docs") or []:
		doc.update(dict.fromkeys(GUEST_HIDDEN_FIELDS))
	if not (docinfo := frappe.response.get("docinfo")):
		return
	# Empty values of the same type keep the form script working.
	kept = frappe._dict(
		{
			key: value
			if key in GUEST_DOCINFO
			else []
			if isinstance(value, list)
			else {}
			if isinstance(value, dict)
			else None
			for key, value in docinfo.items()
		}
	)
	# The form gives read on a document of another owner from the share of the user.
	kept.shared = [_own_share(s) for s in docinfo.get("shared") or [] if s.get("user") == frappe.session.user]
	kept.user_info = {}
	frappe.utils.add_user_info(frappe.session.user, kept.user_info)
	frappe.response["docinfo"] = kept


def _own_share(share: Any) -> frappe._dict:
	"""The share of the user, without the user who shared it."""
	return frappe._dict({key: share.get(key) for key in SHARE_FIELDS})


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
