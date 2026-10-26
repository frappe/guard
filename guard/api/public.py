"""Links that GitHub users click from comments. No login needed: the link is signed."""

import frappe
from frappe import _
from frappe.utils import escape_html

from guard.review import links
from guard.utils import cache, db

RATE_SECONDS = 120


# The link in the summary comment is signed with HMAC. Only a valid signature passes.
# GET only shows a button. Link scanners and crawlers open every link in a comment,
# so only the POST from that button starts a review.
@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])  # nosemgrep: guest-whitelisted-method
def retrigger(pull_request: str, sig: str = "") -> None:
	if not links.verify(pull_request, sig) or not frappe.db.exists("Pull Request", pull_request):
		frappe.throw(_("This link is not valid."), frappe.PermissionError)
	if frappe.request.method != "POST":
		_confirm(pull_request, sig)
		return
	key = f"guard:retrigger:{pull_request}"
	if cache.get(key):
		_page(_("A review started less than two minutes ago. Try again soon."))
		return
	cache.set(key, 1, RATE_SECONDS)
	frappe.set_user("Administrator")  # nosemgrep: frappe-setuser -- after the signature check
	from guard.review.trigger import consider

	review = consider(pull_request, "retrigger", manual=True)
	_page(
		_("Guard is reviewing the pull request again.")
		if review
		else _("Guard skipped the review. Check the repository settings.")
	)


def _confirm(pull_request: str, sig: str) -> None:
	fields = {"pull_request": pull_request, "sig": sig}
	if frappe.session.user != "Guest":
		# A browser that is logged in to the site must send its CSRF token with the POST.
		fields["csrf_token"] = frappe.sessions.get_csrf_token()
	inputs = "".join(
		f"<input type='hidden' name='{name}' value='{escape_html(value)}'>" for name, value in fields.items()
	)
	button = f"<button type='submit' class='btn btn-primary btn-sm'>{_('Review again')}</button>"
	_page(
		_("Review this pull request again?"),
		f"<br><br><form method='post' action='/api/method/guard.api.public.retrigger'>{inputs}{button}</form>",
	)


def _page(message: str, action: str = "") -> None:
	url = str(db.value("Pull Request", str(frappe.form_dict.get("pull_request")), "web_url") or "/")
	frappe.respond_as_web_page(
		_("Guard"),
		f"{message}{action}<br><br><a href='{escape_html(url)}'>{_('Back to the pull request')}</a>",
		indicator_color="blue",
	)
