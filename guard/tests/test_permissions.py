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
		self.assertTrue(permissions.custom_context(team_rule, "write", "lead@example.com"))
		self.assertFalse(permissions.custom_context(team_rule, "write", "dev@example.com"))
		self.assertFalse(permissions.custom_context(org_rule, "write", "lead@example.com"))
		self.assertTrue(permissions.custom_context(org_rule, "read", "dev@example.com"))
		self.assertTrue(permissions.custom_context(org_rule, "write", "Administrator"))

	def test_members_can_read_through_frappe(self):
		# The hooks run inside frappe.has_permission, which treats a falsy answer as a denial.
		for doctype, name in (("Review Settings", "Organization"),):
			self.assertTrue(frappe.has_permission(doctype, "read", name, user="dev@example.com"))
		self.assertFalse(
			frappe.has_permission("Review Settings", "write", "Organization", user="dev@example.com")
		)

	def test_review_settings(self):
		org = frappe.get_doc("Review Settings", "Organization")
		self.assertFalse(permissions.review_settings(org, "write", "lead@example.com"))
		team = frappe.get_doc({"doctype": "Review Settings", "scope": "Team", "team": self.team})
		self.assertTrue(permissions.review_settings(team, "write", "lead@example.com"))
		self.assertFalse(permissions.review_settings(team, "write", "dev@example.com"))


class TestViewerOnLogin(IntegrationTestCase):
	def login_as(self, email):
		from guard.access import on_session_creation

		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{"doctype": "User", "email": email, "first_name": "x", "user_type": "Website User"}
			).insert(ignore_permissions=True)
		previous = frappe.session.user
		frappe.session.user = email
		try:
			on_session_creation()
		finally:
			frappe.session.user = previous
		return frappe.get_doc("User", email)

	def test_company_domain_gets_viewer(self):
		frappe.db.set_single_value("Guard Settings", "viewer_domains", "frappe.io\nerpnext.com")
		user = self.login_as("new-dev@erpnext.com")
		self.assertEqual(user.get("user_type"), "System User")
		self.assertIn("Guard Viewer", frappe.get_roles(user.name))

	def test_other_domain_gets_nothing(self):
		frappe.db.set_single_value("Guard Settings", "viewer_domains", "frappe.io")
		user = self.login_as("someone@gmail.com")
		self.assertEqual(user.get("user_type"), "Website User")
		self.assertNotIn("Guard Viewer", frappe.get_roles(user.name))
