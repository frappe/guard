from dataclasses import replace
from typing import Any

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from guard.llm.client import ChatRequest, LLMClient, use_responder
from guard.llm.embeddings import Embedder
from guard.llm.types import ChatResult, Message
from guard.review import pull_requests, trigger
from guard.review.pipeline import graph_verdict
from guard.review.run import ReviewRun
from guard.tests.cases import PullRequestCase
from guard.tests.test_ai_providers import FIELDS

API = """import frappe


@frappe.whitelist(methods=["POST"])
def find(title):
	return frappe.db.sql(f"select name from tabNote where title = {title}")
"""


def set_settings(**values: Any) -> None:
	for field, value in values.items():
		frappe.db.set_single_value("Guard Settings", field, value)
	frappe.clear_document_cache("Guard Settings", "Guard Settings")


def no_calls(request: ChatRequest) -> ChatResult:
	raise AssertionError(f"offline mode called a model for {request.purpose}")


class OfflineCase(IntegrationTestCase):
	"""A site with a gateway that offline mode must not call."""

	def setUp(self):
		super().setUp()
		for field in (*FIELDS, "offline_mode"):
			self.addCleanup(set_settings, **{field: None})
		name = "Offline Test Provider"
		if not frappe.db.exists("AI Provider", name):
			frappe.get_doc(
				{
					"doctype": "AI Provider",
					"provider_name": name,
					"url": "https://ai.test",
					"dialect": "openai",
					"api_key": "key",
				}
			).insert(ignore_permissions=True)
		set_settings(default_ai_provider=name, base_model="m", embedding_model="e")


class TestOfflineMode(OfflineCase):
	def test_offline_mode_takes_every_route_offline(self):
		self.assertTrue(LLMClient("base").online)
		self.assertTrue(Embedder().uses_model)
		set_settings(offline_mode=1)
		self.assertFalse(LLMClient("base").online)
		self.assertFalse(LLMClient.fast().online)
		self.assertFalse(Embedder().uses_model)
		# Not even the responder of a test answers.
		with use_responder(no_calls):
			self.assertEqual(LLMClient("base").chat([Message("user", "hi")]).content, "")

	def test_a_question_gets_the_offline_answer(self):
		from guard.conversation.answer import answer

		set_settings(offline_mode=1)
		run = ReviewRun(None, None, "", lambda path: None, lambda path: None, lambda path: [], "", "")
		reply = answer(run, "Why?")
		self.assertIn("offline mode", reply.text)


class TestGraphVerdict(UnitTestCase):
	def test_a_review_without_a_model_is_never_five(self):
		# 5/5 auto-approves. Checks that need no model cannot vouch for a change.
		run = ReviewRun(None, None, "", lambda path: None, lambda path: None, lambda path: [], "", "")
		run.offline = True
		verdict = graph_verdict(run)
		self.assertEqual((verdict.confidence_score, verdict.risk_level), (4, "low"))
		self.assertIn("offline mode", verdict.summary)


class TestOfflineReview(PullRequestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(set_settings, offline_mode=None)
		# A Frappe app, so that the Frappe review pack runs.
		files = {"app/hooks.py": 'app_name = "app"\n', "app/modules.txt": "App\n", "app/api.py": API}
		self.info = replace(self.info, head_sha=self.repo.commit(files, "add find"))
		self.adapter.info = self.info
		pull_requests.upsert(self.repository, self.info)
		set_settings(offline_mode=1)

	def test_a_review_posts_only_checks_and_calls_no_model(self):
		with use_responder(no_calls):
			name = trigger.consider(self.pr, "open")
		review = self.row(name)
		self.assertEqual(review["status"], "Completed", review["error"])
		self.assertEqual(review["credits"], 0)
		self.assertEqual(frappe.db.get_value("Code Review", name, "tier"), "base")
		posted = self.adapter.named("create_review")[0]
		self.assertEqual([(c.path, c.line) for c in posted], [("app/api.py", 6)])
		self.assertIn("SQL built from strings", posted[0].body)
		summary = self.adapter.named("post_comment")[0]
		self.assertIn("Guard is in offline mode", summary)
