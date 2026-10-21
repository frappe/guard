import frappe
from frappe.tests import IntegrationTestCase

from guard import permissions


class TestPermissions(IntegrationTestCase):
	def setUp(self):
		for email, role in (("lead@example.com", "Guard Member"), ("dev@example.com", "Guard Member")):
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": email.split("@")[0],
						"send_welcome_email": 0,
						"roles": [{"role": role}],
					}
				).insert(ignore_permissions=True)
		if not frappe.db.exists("Code Provider", "Test Provider"):
			frappe.get_doc(
				{"doctype": "Code Provider", "title": "Test Provider", "provider_type": "GitHub"}
			).insert(ignore_permissions=True)
		self.team = (
			frappe.get_doc(
				{
					"doctype": "Guard Team",
					"team_name": "core",
					"code_provider": "Test Provider",
					"namespace": "core",
					"members": [
						{"user": "lead@example.com", "role": "Admin"},
						{"user": "dev@example.com", "role": "Member"},
					],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_team_rules(self):
		team_rule = frappe.get_doc(
			{"doctype": "Custom Context", "context_type": "Rule", "body": "x", "team": self.team}
		)
		org_rule = frappe.get_doc({"doctype": "Custom Context", "context_type": "Rule", "body": "x"})
		self.assertIsNone(permissions.custom_context(team_rule, "write", "lead@example.com"))
		self.assertFalse(permissions.custom_context(team_rule, "write", "dev@example.com"))
		self.assertFalse(permissions.custom_context(org_rule, "write", "lead@example.com"))
		self.assertIsNone(permissions.custom_context(org_rule, "read", "dev@example.com"))
		self.assertIsNone(permissions.custom_context(org_rule, "write", "Administrator"))

	def test_review_settings(self):
		org = frappe.get_doc("Review Settings", "Organization")
		self.assertFalse(permissions.review_settings(org, "write", "lead@example.com"))
		team = frappe.get_doc({"doctype": "Review Settings", "scope": "Team", "team": self.team})
		self.assertIsNone(permissions.review_settings(team, "write", "lead@example.com"))
		self.assertFalse(permissions.review_settings(team, "write", "dev@example.com"))
