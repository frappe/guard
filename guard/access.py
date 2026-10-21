"""Give company users read access to Guard when they log in."""

import frappe

ROLE = "Guard Viewer"
GUARD_ROLES = {"Guard Admin", "Guard Member", ROLE}


def on_session_creation(login_manager=None) -> None:
	"""A user on an allowed domain without a Guard role becomes a Desk user with Guard Viewer."""
	user = str(frappe.session.user or "")
	if not user or user in ("Administrator", "Guest") or not _allowed(user):
		return
	if GUARD_ROLES & set(frappe.get_roles(user)):
		return
	doc = frappe.get_doc("User", user)
	doc.set("user_type", "System User")
	doc.append("roles", {"role": ROLE})
	doc.save(ignore_permissions=True)


def _allowed(email: str) -> bool:
	text = str(frappe.db.get_single_value("Guard Settings", "viewer_domains") or "")
	domains = {line.strip().lower().lstrip("@") for line in text.splitlines() if line.strip()}
	return "@" in email and email.rsplit("@", 1)[1].lower() in domains
