from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from guard.llm.client import use_responder
from guard.providers.base import ProviderComment, PullRequestInfo
from guard.review import pull_requests, trigger
from guard.tests.fakes import FakeAdapter, scripted
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db

MODELS = """def get_user(user_id):
    return {"id": user_id}


def update_user_email(user, email):
    user["email"] = email
    return user
"""

SERVICE = """from app.models import get_user, update_user_email


def change_email(user_id, email):
    user = get_user(user_id)
    return update_user_email(user, email)
"""

PATCHED = (
	"guard.review.pipeline",
	"guard.review.trigger",
	"guard.learning.approve",
	"guard.conversation.threads",
	"guard.learning.addressed",
)


class TestPipeline(IntegrationTestCase):
	def setUp(self):
		frappe.db.set_single_value("Guard Settings", "learn_from_activity", 1)
		self.repo = TempRepo({"app/__init__.py": "", "app/models.py": MODELS, "app/service.py": SERVICE})
		self.base = self.repo.head()
		self.repository = make_repository(self.repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		self.repo.branch_off("feature")
		self.head = self.repo.commit(
			{
				"app/models.py": MODELS.replace(
					"def update_user_email(user, email):", "def update_user_email(user, email, notify):"
				)
			},
			"add notify",
		)
		self.info = PullRequestInfo(
			7,
			"Add notify flag",
			"Adds a flag.",
			"ada",
			"open",
			False,
			"main",
			"feature",
			self.base,
			self.head,
			"https://github.test/pr/7",
		)
		self.adapter = FakeAdapter(self.info)
		self.stack = ExitStack()
		for module in PATCHED:
			self.stack.enter_context(patch(f"{module}.adapter_for_repository", return_value=self.adapter))
		self.pr = str(pull_requests.upsert(self.repository, self.info).name)

	def tearDown(self):
		self.stack.close()
		self.repo.cleanup()

	def row(self, name):
		self.assertTrue(name)
		return (
			db.values(
				"Code Review",
				str(name),
				("status", "error", "posted_count", "confidence_score", "credits", "review_number"),
			)
			or {}
		)

	def review(self, steps):
		with use_responder(scripted(steps)):
			return trigger.consider(self.pr, "open")

	def test_review_posts_findings_summary_and_status(self):
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Caller change_email is not updated for the new notify argument",
			"body": "`app/service.py:6` calls `update_user_email(user, email)`. It raises TypeError now.",
			"confidence": 0.9,
		}
		name = self.review(
			[
				[("read_file", {"path": "app/service.py"})],
				[
					("report_finding", finding),
					(
						"submit_review",
						{"summary": "Adds a notify flag.", "confidence_score": 5, "risk_level": "medium"},
					),
				],
			]
		)
		review = self.row(name)
		self.assertEqual(review["status"], "Completed", review["error"])
		self.assertEqual(review["posted_count"], 1)
		self.assertEqual(review["confidence_score"], 3)  # capped by the P1
		self.assertEqual(review["credits"], 1)
		posted = self.adapter.named("create_review")[0]
		self.assertEqual(posted[0].path, "app/models.py")
		self.assertIn("**P1**", posted[0].body)
		summary = self.adapter.named("post_comment")[0]
		self.assertIn("<!-- guard:summary -->", summary)
		self.assertIn("Confidence **3/5**", summary)
		self.assertIn("Affects 1 caller in 1 file. 1 caller may break.", summary)
		self.assertIn("<summary><b>Impact</b></summary>", summary)
		self.assertIn("-.->|may break|", summary)
		self.assertIn("Reviews (1)", summary)
		self.assertEqual(self.adapter.named("status"), ["pending", "failure"])
		self.assertIn("+1", self.adapter.named("react"))

	def test_rereview_edits_summary_and_dedupes(self):
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers are not updated",
			"body": "x",
			"confidence": 0.9,
		}
		self.review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 4})]]
		)
		with use_responder(
			scripted(
				[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 4})]]
			)
		):
			second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		review = self.row(second)
		self.assertEqual(review["status"], "Completed", review["error"])
		self.assertEqual(review["posted_count"], 0)
		self.assertEqual(review["review_number"], 2)
		self.assertEqual(len(self.adapter.named("update_comment")), 1)
		# Raised again, so it still applies: the thread stays open.
		self.assertFalse(self.adapter.named("resolve"))

	def test_rereview_resolves_fixed_findings(self):
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers are not updated",
			"body": "x",
			"confidence": 0.9,
		}
		self.review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 3})]]
		)
		with (
			patch("guard.review.stale._state", return_value="fixed"),
			use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])),
		):
			trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		self.assertEqual(
			frappe.get_all("Review Finding", filters={"pull_request": self.pr}, pluck="status"), ["Addressed"]
		)
		self.assertEqual(len(self.adapter.named("resolve")), 1)
		self.assertEqual(self.adapter.named("status")[-1], "success")

	def test_rereview_drops_new_p2(self):
		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		nit = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P2",
			"comment_type": "style",
			"title": "Name is vague",
			"body": "x",
			"confidence": 0.9,
		}
		with use_responder(
			scripted([[("report_finding", nit), ("submit_review", {"summary": "s", "confidence_score": 4})]])
		):
			second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		self.assertEqual(self.row(second)["posted_count"], 0)
		# Only a dropped P2 was left, so 4 becomes 5.
		self.assertEqual(self.row(second)["confidence_score"], 5)

	def test_gate_skips_drafts_and_disabled_repositories(self):
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 1)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 0)
		frappe.db.set_value("Code Repository", self.repository, "reviews_enabled", 0)
		self.assertIsNone(trigger.consider(self.pr, "open"))

	def test_auto_approve_clean_low_risk(self):
		self.repo.git("checkout", "-q", "main")
		self.repo.commit(
			{".guard/config.toml": '[auto_approve]\nenabled = true\nrisk_ceiling = "low"\n'}, "policy"
		)
		self.repo.git("checkout", "-q", "feature")
		self.repo.git("merge", "-q", "main", "-m", "merge main")
		frappe.db.set_value("Pull Request", self.pr, "head_sha", self.repo.head())
		name = self.review(
			[[("submit_review", {"summary": "Small change.", "confidence_score": 5, "risk_level": "low"})]]
		)
		self.assertEqual(frappe.db.get_value("Code Review", name, "status"), "Completed")
		self.assertTrue(self.adapter.named("approve"))
		self.assertEqual(self.adapter.named("status")[-1], "success")
		self.assertTrue(db.value("Pull Request", self.pr, "guard_approved"))

	def test_mention_answers_question(self):
		from guard.conversation import threads

		comment = ProviderComment(
			"c1", "issue", "ada", "@guard why does this change the signature?", number=7
		)
		with use_responder(
			scripted([[("read_file", {"path": "app/models.py"})], "It adds `notify` to `update_user_email`."])
		):
			with patch("guard.llm.client.LLMClient.online", new=True):
				threads.handle(self.pr, comment)
		self.assertEqual(self.adapter.named("reply"), ["It adds `notify` to `update_user_email`."])

	def test_polling_opens_and_syncs(self):
		from guard.webhooks import polling

		frappe.db.set_single_value("Guard Settings", "polling_enabled", 1)
		frappe.delete_doc("Pull Request", self.pr, force=True)
		self.adapter.list_open_pull_requests = lambda repo: [self.info]
		self.adapter.list_comments = lambda repo, number: []
		with (
			patch("guard.webhooks.polling.adapter_for_repository", return_value=self.adapter),
			use_responder(scripted([])),
		):
			polling.poll_repository(self.repository)
		pr = db.value("Pull Request", {"repository": self.repository, "number": 7}, "name")
		self.assertTrue(pr)
		self.assertEqual(frappe.db.count("Code Review", {"pull_request": pr, "trigger": "open"}), 1)
		with (
			patch("guard.webhooks.polling.adapter_for_repository", return_value=self.adapter),
			use_responder(scripted([])),
		):
			polling.poll_repository(self.repository)
		self.assertEqual(frappe.db.count("Code Review", {"pull_request": pr}), 1)
