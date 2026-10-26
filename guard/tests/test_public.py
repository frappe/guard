from types import SimpleNamespace
from unittest.mock import patch

import frappe

from guard.api import public
from guard.review import links
from guard.tests.cases import PullRequestCase


class TestRetriggerLink(PullRequestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(setattr, frappe.local, "request", getattr(frappe.local, "request", None))
		self.addCleanup(frappe.set_user, "Administrator")
		self.consider = self.stack.enter_context(patch("guard.review.trigger.consider", return_value="CR"))

	def open(self, method, **form):
		frappe.local.request = SimpleNamespace(method=method)
		frappe.local.response = frappe._dict()
		frappe.local.form_dict = frappe._dict(form)
		public.retrigger(**form)

	def test_get_shows_a_button_and_does_not_review(self):
		frappe.set_user("Guest")
		self.open("GET", pull_request=self.pr, sig=links.sign(self.pr))
		self.consider.assert_not_called()
		self.assertEqual(frappe.local.response["type"], "page")
		self.assertIn("<form method='post'", str(frappe.local.message))
		self.assertIn(f"name='sig' value='{links.sign(self.pr)}'", str(frappe.local.message))
		self.assertNotIn("csrf_token", str(frappe.local.message))

	def test_get_from_a_logged_in_browser_sends_the_csrf_token(self):
		self.open("GET", pull_request=self.pr, sig=links.sign(self.pr))
		self.assertIn("name='csrf_token'", str(frappe.local.message))

	def test_post_from_the_button_reviews_again(self):
		frappe.set_user("Guest")
		self.open("POST", pull_request=self.pr, sig=links.sign(self.pr))
		self.consider.assert_called_once_with(self.pr, "retrigger", manual=True)

	def test_link_without_a_signature_is_not_valid(self):
		# Some link scanners read `&amp;sig=` from the comment HTML and send `amp;sig` instead of `sig`.
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			self.open("GET", pull_request=self.pr)
		self.consider.assert_not_called()
