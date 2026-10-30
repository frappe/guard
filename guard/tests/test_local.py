from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from guard import install
from guard.git.repository import store_for
from guard.git.store import GitError
from guard.indexing.jobs import ensure_fresh
from guard.llm.client import use_responder
from guard.review import local
from guard.tests.cases import MODELS, SERVICE
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db

NEW_SIGNATURE = MODELS.replace(
	"def update_user_email(user, email):", "def update_user_email(user, email, notify):"
)
# An AWS key, which the secret check finds.
KEY_LINE = 'BACKUP_KEY = "AKIAABCDEFGHIJKLMNOP"\n'


class TestLocalReview(IntegrationTestCase):
	def setUp(self):
		self.repo = TempRepo({"app/__init__.py": "", "app/models.py": MODELS, "app/service.py": SERVICE})
		self.full_name = f"acme/{frappe.generate_hash(length=6)}"
		self.repository = make_repository(self.repo, full_name=self.full_name)
		ensure_fresh(self.repository)
		self.base = self.repo.head()
		self.stack = ExitStack()
		# A local review runs no model and embeds nothing: the agent of the developer pays.
		self.stack.enter_context(use_responder(self.no_model))
		self.stack.enter_context(patch("guard.llm.embeddings.Embedder.embed", side_effect=self.no_model))

	def tearDown(self):
		self.stack.close()
		self.repo.cleanup()

	def no_model(self, *args, **kwargs):
		raise AssertionError("a local review called a model")

	def working_tree_diff(self) -> str:
		"""Change the signature in the working tree, without a commit, and add a new file."""
		(self.repo.path / "app/models.py").write_text(NEW_SIGNATURE + KEY_LINE)
		(self.repo.path / "app/notes.py").write_text("NOTE = 1\n")
		self.repo.git("add", "-N", "app/notes.py")
		return self.repo.git("diff", "--binary", self.base) + "\n"

	def start(self, diff: str | None = None) -> dict:
		return local.start(
			f"git@github.com:{self.full_name}.git", self.base, diff or self.working_tree_diff(), "feature"
		)

	def test_apply_writes_the_tree_of_the_base_with_the_patch(self):
		store = store_for(self.repository)
		store.fetch_sha(self.base)
		tree = store.apply(self.base, self.working_tree_diff())
		self.assertEqual(store.show(tree, "app/models.py"), NEW_SIGNATURE + KEY_LINE)
		self.assertEqual(store.show(tree, "app/notes.py"), "NOTE = 1\n")
		self.assertEqual(store.refs("refs/heads/"), ["refs/heads/main"])
		with self.assertRaises(GitError):
			store.apply(
				self.base,
				"diff --git a/app/models.py b/app/models.py\n--- a/app/models.py\n+++ b/app/models.py\n@@ -1,1 +1,1 @@\n-nothing here\n+x\n",
			)

	def test_start_gives_the_graph_the_rules_and_the_checks(self):
		started = self.start()
		self.assertEqual(started["repository"], self.full_name)
		self.assertEqual(started["files"], 2)
		self.assertIn("AWS access key", " ".join(f["title"] for f in started["findings"]))
		guide = started["guide"]
		self.assertIn("update_user_email", guide)
		self.assertIn("app/service.py", guide)
		self.assertIn(started["review_id"], guide)
		self.assertIn("guard.api.local.submit", guide)
		# Nothing is saved before the agent submits.
		self.assertFalse(frappe.db.exists("Code Review", {"repository": self.repository}))

	def test_submit_saves_a_local_review_and_posts_nothing(self):
		started = self.start()
		line = NEW_SIGNATURE.splitlines().index("def update_user_email(user, email, notify):") + 1
		result = local.submit(
			started["review_id"],
			[
				{
					"path": "app/models.py",
					"start_line": line,
					"end_line": line,
					"severity": "P1",
					"comment_type": "logic",
					"title": "change_email does not pass notify",
					"body": "app/service.py calls update_user_email with two arguments.",
				},
				{
					"path": "app/service.py",
					"start_line": 6,
					"severity": "P1",
					"title": "Caller misses the new argument",
					"body": "change_email calls update_user_email with two arguments.",
				},
				{
					"path": "app/missing.py",
					"start_line": 1,
					"severity": "P2",
					"title": "Not in the change",
					"body": "x",
				},
			],
			summary="- Adds a notify flag.",
			confidence_score=5,
			risk_level="low",
		)
		review = (
			db.values(
				"Code Review",
				result["review"],
				("source", "status", "credits", "confidence_score", "findings_count", "pull_request"),
			)
			or {}
		)
		self.assertEqual(review["source"], "local")
		self.assertEqual(review["status"], "Completed")
		self.assertEqual(review["credits"], 0)
		self.assertFalse(review["pull_request"])
		# The open P1 and the secret cap the score of the agent.
		self.assertLess(review["confidence_score"], 5)
		titles = {f["title"] for f in result["kept"]}
		self.assertIn("change_email does not pass notify", titles)
		self.assertTrue(any("AWS access key" in t for t in titles))
		# A finding on an unchanged caller stays. One on a file that does not exist goes.
		self.assertIn("Caller misses the new argument", titles)
		self.assertNotIn("Not in the change", titles)
		self.assertIn("Not in the change", {f["title"] for f in result["dropped"]})
		rows = frappe.get_all("Review Finding", filters={"code_review": result["review"]}, fields=["posted"])
		self.assertEqual(len(rows), len(result["kept"]) + len(result["dropped"]))
		self.assertFalse(any(r.posted for r in rows))

	def test_a_review_is_submitted_once_and_by_the_user_who_started_it(self):
		started = self.start()
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.ValidationError):
				local.submit(started["review_id"], [])
		finally:
			frappe.set_user("Administrator")
		local.submit(started["review_id"], [])
		with self.assertRaises(frappe.ValidationError):
			local.submit(started["review_id"], [])

	def test_a_diff_that_does_not_apply_fails(self):
		diff = "diff --git a/app/models.py b/app/models.py\n--- a/app/models.py\n+++ b/app/models.py\n@@ -1,1 +1,1 @@\n-nothing here\n+x\n"
		with self.assertRaises(frappe.ValidationError):
			self.start(diff)

	def test_an_unknown_repository_fails(self):
		with self.assertRaises(frappe.PermissionError):
			local.start("acme/nothing-here", self.base, self.working_tree_diff())

	def test_symbol_gives_callers_on_the_default_branch(self):
		definitions = local.symbol(self.full_name, "update_user_email")["definitions"]
		self.assertEqual(definitions[0]["path"], "app/models.py")
		self.assertIn("app/service.py", [c["path"] for c in definitions[0]["callers"]])


class TestAgentInstructions(IntegrationTestCase):
	def test_install_fills_empty_agent_instructions_only(self):
		frappe.db.set_single_value("System Settings", "agent_instructions", "")
		install.add_agent_instructions()
		self.assertEqual(
			frappe.db.get_single_value("System Settings", "agent_instructions"), install.AGENT_INSTRUCTIONS
		)
		frappe.db.set_single_value("System Settings", "agent_instructions", "Our own words.")
		install.add_agent_instructions()
		self.assertEqual(
			frappe.db.get_single_value("System Settings", "agent_instructions"), "Our own words."
		)
