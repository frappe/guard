from contextlib import contextmanager
from unittest.mock import patch

import frappe
from frappe.mcp.protocol import McpError
from frappe.share import add as share
from frappe.share import remove as unshare

from guard import permissions
from guard.api import ui, v2
from guard.indexing.jobs import ensure_fresh
from guard.mcp import prompts, tools
from guard.review import crossrepo, local
from guard.tests.cases import MODELS
from guard.tests.test_local import LocalCase
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db

GUEST = "guard-guest@example.com"
MEMBER = "guard-guest-member@example.com"
VIEWER = "guard-guest-viewer@example.com"
# Guard Guest with a team role is not a guest.
BOTH = "guard-guest-both@example.com"
PULL_REQUEST_TOOLS = {"pr_findings", "finding_feedback", "request_review", "publish_review"}


class TestGuardGuest(LocalCase):
	"""An outside collaborator: the MCP server and the graph of the given repositories, and nothing of
	pull requests or people."""

	def setUp(self):
		# Index the other repository first: LocalCase stops embeddings.
		self.other_repo = TempRepo({"lib/__init__.py": "", "lib/models.py": MODELS})
		self.addCleanup(self.other_repo.cleanup)
		self.other = make_repository(self.other_repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		ensure_fresh(self.other)
		super().setUp()
		for email, roles in (
			(GUEST, ["Guard Guest"]),
			(MEMBER, ["Guard Member"]),
			(VIEWER, ["Guard Viewer"]),
			(BOTH, ["Guard Guest", "Guard Member"]),
		):
			if not frappe.db.exists("User", email):
				frappe.get_doc(
					{
						"doctype": "User",
						"email": email,
						"first_name": email.split("@")[0],
						"send_welcome_email": 0,
						"roles": [{"role": role} for role in roles],
					}
				).insert(ignore_permissions=True)
		# The tests of a class share one transaction, and the cache of User Permissions outlives it.
		frappe.db.delete("User Permission", {"user": GUEST})
		frappe.db.delete("DocShare", {"user": GUEST})
		frappe.clear_cache(user=GUEST)
		self.addCleanup(frappe.clear_cache, user=GUEST)

	def grant(self, repository: str, applicable_for: str = "") -> None:
		frappe.get_doc(
			{
				"doctype": "User Permission",
				"user": GUEST,
				"allow": "Code Repository",
				"for_value": repository,
				"apply_to_all_doctypes": 0 if applicable_for else 1,
				"applicable_for": applicable_for or None,
			}
		).insert(ignore_permissions=True)

	@contextmanager
	def as_user(self, user: str = GUEST):
		frappe.set_user(user)
		try:
			yield
		finally:
			frappe.set_user("Administrator")

	def test_a_guest_sees_only_the_given_repositories_and_their_graph(self):
		self.assertTrue(permissions.is_guest(GUEST))
		for user in (MEMBER, BOTH, "Administrator"):
			self.assertFalse(permissions.is_guest(user), user)
			self.assertEqual(permissions.repository_query(user, "Code Repository"), "")
		# A User Permission for another DocType gives no repository.
		self.grant(self.repository, applicable_for="Code Review")
		with self.as_user():
			# Frappe applies no User Permission to a user who has none: Guard denies all.
			self.assertEqual(frappe.get_list("Code Repository", pluck="name"), [])
			self.assertEqual(frappe.get_list("Code Entity", pluck="name", limit=1), [])
			self.assertFalse(frappe.has_permission("Code Repository", "read", self.repository))
		self.grant(self.repository)
		own_index = str(db.value("Code Repository", self.repository, "default_index"))
		other_index = str(db.value("Code Repository", self.other, "default_index"))
		with self.as_user():
			self.assertEqual(frappe.get_list("Code Repository", pluck="name"), [self.repository])
			self.assertTrue(frappe.has_permission("Code Repository", "read", self.repository))
			self.assertFalse(frappe.has_permission("Code Repository", "read", self.other))
			self.assertFalse(frappe.has_permission("Code Repository", "write", self.repository))
			for doctype in ("Code Entity", "Code Edge", "Code Chunk"):
				indexes = set(frappe.get_list(doctype, pluck="index", limit=1000))
				self.assertEqual(indexes, {own_index}, doctype)
			self.assertEqual(set(frappe.get_list("Repository Index", pluck="name")), {own_index})
			self.assertFalse(frappe.has_permission("Repository Index", "read", other_index))
			entity = frappe.get_all("Code Entity", filters={"index": other_index}, pluck="name", limit=1)
			self.assertFalse(frappe.has_permission("Code Entity", "read", entity[0]))
			# The Desk and API methods check the same permission.
			with self.assertRaises(frappe.PermissionError):
				ui.explain_config(self.other)
			with self.assertRaises(frappe.PermissionError):
				v2.get_repository(f"github:main:{self.other_repo_name()}")
			with self.assertRaises(frappe.PermissionError):
				v2.search("update_user_email", [self.other_repo_name()])
			found = tools.call({"name": "find_symbol", "arguments": {"name": "update_user_email"}})
			self.assertEqual(
				{d["repository"] for d in found["structuredContent"]["definitions"]}, {self.full_name}
			)
			denied = tools.call({"name": "team_rules", "arguments": {"repository": self.other_repo_name()}})
			self.assertTrue(denied["isError"])
		# Members see every repository, as before.
		for user in (MEMBER, BOTH):
			with self.as_user(user):
				self.assertTrue(frappe.has_permission("Code Repository", "read", self.other))
				self.assertIn(self.other, frappe.get_list("Code Repository", pluck="name", limit=1000))

	def test_a_share_gives_a_guest_the_repository_and_its_graph(self):
		# Only Guard Admin and System Manager can share a repository. A Guard Admin cannot add User
		# Permissions, so a share is how they give a repository.
		share("Code Repository", self.repository, GUEST, read=1)
		own_index = str(db.value("Code Repository", self.repository, "default_index"))
		with self.as_user():
			self.assertEqual(permissions.guest_repositories(), [self.repository])
			self.assertEqual(
				tools.call({"name": "list_repositories", "arguments": {}})["structuredContent"][
					"repositories"
				],
				[self.full_name],
			)
			self.assertTrue(frappe.has_permission("Code Repository", "read", self.repository))
			self.assertFalse(frappe.has_permission("Code Repository", "read", self.other))
			# A guest cannot share the repository on, also not with everyone.
			with self.assertRaises(frappe.PermissionError):
				share("Code Repository", self.repository, everyone=1, flags={"ignore_share_permission": 1})
			self.assertEqual(set(frappe.get_list("Code Entity", pluck="index", limit=1000)), {own_index})
			self.assertTrue(frappe.has_permission("Repository Index", "read", own_index))
			found = tools.call({"name": "find_symbol", "arguments": {"name": "update_user_email"}})
			self.assertEqual(
				{d["repository"] for d in found["structuredContent"]["definitions"]}, {self.full_name}
			)
		# A User Permission and a share give the repository once.
		self.grant(self.repository)
		self.assertEqual(permissions.guest_repositories(GUEST), [self.repository])
		frappe.db.delete("User Permission", {"user": GUEST})
		frappe.clear_cache(user=GUEST)
		unshare("Code Repository", self.repository, GUEST, flags={"ignore_share_permission": 1})
		with self.as_user():
			self.assertEqual(
				tools.call({"name": "list_repositories", "arguments": {}})["structuredContent"][
					"repositories"
				],
				[],
			)
			self.assertEqual(frappe.get_list("Code Entity", pluck="name", limit=1), [])

	def test_a_guest_sees_no_other_users_in_a_form(self):
		share("Code Repository", self.repository, GUEST, read=1)
		share("Code Repository", self.repository, MEMBER, read=1)
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Comment",
				"reference_doctype": "Code Repository",
				"reference_name": self.repository,
				"content": "ping",
				"comment_email": MEMBER,
			}
		).insert(ignore_permissions=True)

		def method(name: str):
			return frappe.get_attr(frappe.override_whitelisted_method(name))

		def docinfo(user: str) -> dict:
			frappe.local.response = frappe._dict({"docs": []})
			with self.as_user(user):
				method("frappe.desk.form.load.getdoc")("Code Repository", self.repository)
			return frappe.response["docinfo"]

		theirs = docinfo(MEMBER)
		self.assertTrue(theirs["shared"])
		self.assertTrue(theirs["comments"])
		mine = docinfo(GUEST)
		for key in ("comments", "versions", "views", "assignments", "communications"):
			self.assertEqual(mine[key], [], key)
		# The form reads the share of the guest to give them read, so it stays, without who shared it.
		self.assertEqual([s["user"] for s in mine["shared"]], [GUEST])
		self.assertNotIn("owner", mine["shared"][0])
		self.assertEqual(set(mine["user_info"]), {GUEST})
		self.assertEqual(mine["name"], self.repository)
		self.assertIsNone(frappe.response["docs"][0].get("_comments"))
		with self.as_user():
			shares = method("frappe.share.get_users")("Code Repository", self.repository)
			self.assertEqual([s["user"] for s in shares], [GUEST])
			viewers = method("frappe.desk.form.load.get_user_info_for_viewers")([GUEST, MEMBER])
			self.assertEqual(set(viewers), {GUEST})

	def test_a_guest_gets_no_write_from_a_share(self):
		with self.assertRaises(frappe.ValidationError):
			share("Code Repository", self.repository, GUEST, read=1, write=1)
		# A write share from before this check, or with everyone, gives write in Frappe. The controller
		# still refuses the save.
		name = str(share("Code Repository", self.repository, GUEST, read=1).name)
		db.set_values("DocShare", name, {"write": 1})
		with self.as_user():
			self.assertTrue(frappe.has_permission("Code Repository", "write", self.repository))
			doc = frappe.get_doc("Code Repository", self.repository)
			doc.set("default_branch", "evil")
			with self.assertRaises(frappe.PermissionError):
				doc.save()
		# Others save as before.
		doc = frappe.get_doc("Code Repository", self.repository)
		doc.set("default_branch", doc.get("default_branch"))
		doc.save()

	def test_a_guest_sees_no_pull_requests_and_no_people(self):
		self.grant(self.repository)
		with self.as_user():
			for doctype in (
				"Pull Request",
				"Pull Request Comment",
				"Review Feedback",
				"Guard User",
				"Guard Team",
				"Code Provider",
				"Custom Context",
				"Review Settings",
				"Guard Settings",
				"Chat Session",
			):
				self.assertFalse(frappe.has_permission(doctype, "read"), doctype)
			self.assertEqual(frappe.get_list("User", pluck="name"), [GUEST])
			mentions = frappe.get_attr(
				frappe.override_whitelisted_method("frappe.desk.search.get_names_for_mentions")
			)
			self.assertEqual(mentions("guard"), [])
			# Guard's model is not for guests.
			with self.assertRaises(frappe.PermissionError):
				v2.query([{"role": "user", "content": "where?"}], [self.full_name])
			with self.assertRaises(frappe.PermissionError):
				ui.improve_rule("be nice")
		with self.as_user(MEMBER):
			self.assertIn(GUEST, frappe.get_list("User", pluck="name", limit=1000))

	def test_a_guest_gets_no_tools_for_pull_requests(self):
		self.grant(self.repository)
		with self.as_user():
			names = {t["name"] for t in tools.definitions()}
			self.assertFalse(names & PULL_REQUEST_TOOLS)
			self.assertIn("start_review", names)
			self.assertIn("search_code", names)
			self.assertNotIn("fix", {p["name"] for p in prompts.definitions()})
			self.assertNotIn("pr_findings", tools.instructions())
			self.assertIn("install_hook", tools.instructions())
			answer = tools.call({"name": "pr_findings", "arguments": {"repository": self.full_name}})
			self.assertTrue(answer["isError"])
			self.assertIn("cannot use pr_findings", answer["content"][0]["text"])
			with self.assertRaises(McpError):
				prompts.get({"name": "fix"})
		with self.as_user(MEMBER):
			self.assertTrue(PULL_REQUEST_TOOLS <= {t["name"] for t in tools.definitions()})
		# A viewer reads pull requests, but cannot dismiss findings.
		with self.as_user(VIEWER):
			names = {t["name"] for t in tools.definitions()}
			self.assertIn("pr_findings", names)
			self.assertNotIn("finding_feedback", names)
			# A review on a pull request is for reviewer roles only.
			self.assertFalse({"request_review", "publish_review"} & names)
			answer = tools.call({"name": "request_review", "arguments": {"repository": self.full_name}})
			self.assertIn("cannot use request_review", answer["content"][0]["text"])
			with self.assertRaises(frappe.PermissionError) as raised:
				ui.rereview("any")
			self.assertIn("Guard Member", str(raised.exception))

	def test_a_guest_without_repositories_is_told_to_ask(self):
		with self.as_user():
			answer = tools.call({"name": "list_repositories", "arguments": {}})["structuredContent"]
		self.assertEqual(answer["repositories"], [])
		self.assertIn("Ask a Guard admin to share the repository", answer["next"])

	def test_a_guest_reviews_locally_and_reads_only_their_own_reviews(self):
		self.grant(self.repository)
		frappe.get_doc(
			{
				"doctype": "Pull Request",
				"title": "Secret plans",
				"repository": self.repository,
				"number": 7,
				"state": "open",
				"head_branch": "feature",
				"head_clone_url": f"https://github.com/{self.full_name}.git",
			}
		).insert(ignore_permissions=True)
		# The other repository uses the changed function, but the guest cannot read it.
		other = crossrepo.OtherRepository(
			str(db.value("Code Repository", self.other, "full_name")),
			self.other,
			str(db.value("Code Repository", self.other, "default_index")),
		)
		with (
			patch("guard.review.crossrepo.linked", return_value=[other]),
			patch("guard.review.crossrepo.attach") as attach,
			self.as_user(),
		):
			started = self.start()
			self.assertEqual(attach.call_args.args[1], [])
			result = local.submit(started["review_id"], [])
			review = str(result["review"])
			self.assertTrue(frappe.has_permission("Code Review", "read", review))
			findings = frappe.get_list("Review Finding", filters={"code_review": review}, pluck="name")
			self.assertTrue(findings)
		self.assertFalse(db.value("Code Review", review, "pull_request"))
		with self.as_user(MEMBER):
			started = self.start()
			theirs = str(local.submit(started["review_id"], [])["review"])
		self.assertTrue(db.value("Code Review", theirs, "pull_request"))
		with self.as_user():
			self.assertFalse(frappe.has_permission("Code Review", "read", theirs))
			self.assertEqual(set(frappe.get_list("Code Review", pluck="name", limit=1000)), {review})

	def other_repo_name(self) -> str:
		return str(db.value("Code Repository", self.other, "full_name"))
