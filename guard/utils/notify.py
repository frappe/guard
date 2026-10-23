"""Notifications to Guard admins."""

import frappe

from guard.utils import cache


def admins(subject: str, body: str, key: str = "", every: int = 3600) -> bool:
	"""Notify Guard Admins, or System Managers when there are none. False when nobody got it.

	With `key`, send at most once in `every` seconds.
	"""
	from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification
	from frappe.utils.user import get_users_with_role

	if key and cache.get(f"guard:notified:{key}"):
		return False
	users = get_users_with_role("Guard Admin") or get_users_with_role("System Manager")
	if not users:
		return False
	enqueue_create_notification(
		users,
		{
			"type": "Alert",
			"subject": subject,
			"email_content": frappe.utils.escape_html(body),
			"document_type": "Guard Settings",
			"document_name": "Guard Settings",
			"from_user": "Administrator",
		},
	)
	if key:
		cache.set(f"guard:notified:{key}", 1, every)
	return True
