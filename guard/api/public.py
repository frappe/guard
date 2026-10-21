"""Links that GitHub users click from comments. No login needed: the link is signed."""

import frappe
from frappe import _

from guard.review import links
from guard.utils import cache

RATE_SECONDS = 120


@frappe.whitelist(allow_guest=True, methods=["GET"])
def retrigger(pull_request: str, sig: str) -> None:
	if not links.verify(pull_request, sig) or not frappe.db.exists("Pull Request", pull_request):
		frappe.throw(_("This link is not valid."), frappe.PermissionError)
	key = f"guard:retrigger:{pull_request}"
	if cache.get(key):
		_page(_("A review started less than two minutes ago. Try again soon."))
		return
	cache.set(key, 1, RATE_SECONDS)
	frappe.set_user("Administrator")
	from guard.review.trigger import consider

	review = consider(pull_request, "retrigger", manual=True)
	_page(
		_("Guard is reviewing the pull request again.")
		if review
		else _("Guard skipped the review. Check the repository settings.")
	)


def _page(message: str) -> None:
	url = frappe.db.get_value("Pull Request", frappe.form_dict.get("pull_request"), "web_url") or "/"
	frappe.respond_as_web_page(
		_("Guard"),
		f"{message}<br><br><a href='{url}'>{_('Back to the pull request')}</a>",
		indicator_color="blue",
	)
