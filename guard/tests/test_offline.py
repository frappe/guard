from dataclasses import replace
from typing import Any
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_to_date, now_datetime

from guard.llm import breaker
from guard.llm.client import ChatRequest, LLMClient, use_responder
from guard.llm.embeddings import Embedder
from guard.llm.types import ChatResult, Message
from guard.review import jobs, pipeline, pull_requests, trigger
from guard.review.pipeline import graph_verdict
from guard.review.run import ReviewRun
from guard.tests.cases import PullRequestCase
from guard.tests.fakes import scripted
from guard.tests.test_ai_providers import FIELDS
from guard.utils import cache, db

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


class TestOutageFallback(PullRequestCase):
	"""An AI provider is down. The blank AI provider is the one of every model in tests."""

	def setUp(self):
		super().setUp()
		self.addCleanup(breaker.reset)
		self.addCleanup(set_settings, outage_fallback_after=None)
		with patch("guard.llm.breaker._notify_admins"):
			for _ in range(breaker.THRESHOLD):
				breaker.record_failure("Gateway answered 502")
		# Tests run jobs inline and ignore delays: a review that waits would run again at once.
		real, self.later = jobs.enqueue_review, []

		def enqueue(review: str, delay: int = 0, attempt: int = 0, tier: str = "") -> None:
			if delay or attempt:
				self.later.append(review)
				return
			real(review, delay=delay, attempt=attempt, tier=tier)

		self.stack.enter_context(patch("guard.review.jobs.enqueue_review", side_effect=enqueue))

	def outage_began(self, minutes_ago: int) -> None:
		since = add_to_date(now_datetime(), minutes=-minutes_ago)
		cache.set(breaker._key(breaker.OUTAGE, ""), {"since": str(since), "reason": "502"}, 3600)

	def others(self, name: str) -> list[dict[str, Any]]:
		return frappe.get_all(
			"Code Review",
			filters={"pull_request": self.pr, "name": ["!=", name]},
			fields=["name", "status", "stage", "trigger", "outage_fallback"],
		)

	def test_a_long_outage_posts_checks_then_a_full_review_follows(self):
		self.outage_began(20)

		def no_embeddings(*args, **kwargs):
			raise AssertionError("an embedding call in an outage")

		with (
			use_responder(no_calls),
			patch("guard.llm.embeddings.Embedder.embed", no_embeddings),
			patch("guard.indexing.vectors.VectorStore.nearest", no_embeddings),
		):
			name = str(trigger.consider(self.pr, "open"))
		review = (
			db.values("Code Review", name, ("status", "error", "credits", "outage_fallback", "summary")) or {}
		)
		self.assertEqual(review["status"], "Completed", review["error"])
		self.assertEqual((review["credits"], review["outage_fallback"]), (0, 1))
		self.assertIn("An AI provider is down", review["summary"])
		# The check stays queued: the full review waits for the AI provider.
		self.assertEqual(self.adapter.named("status")[-1], "queued")
		[follow] = self.others(name)
		self.assertEqual(
			(follow["status"], follow["stage"], follow["trigger"], follow["outage_fallback"]),
			("Queued", pipeline.GATEWAY_DOWN, "open", 0),
		)
		self.assertEqual(self.later, [follow["name"]])
		# A second run of `after_review` does not replace the follow-up that waits.
		self.assertIsNone(jobs.follow_up(name))

		# The AI provider answers: the next try of the full review runs the agent.
		breaker.reset()
		# A P2 on an old line: the full review posts it, as a first review does.
		problem = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P2",
			"comment_type": "style",
			"title": "Name is vague",
			"body": "x",
			"confidence": 0.9,
		}
		verdict = {"summary": "s", "confidence_score": 4}
		with use_responder(scripted([[("report_finding", problem), ("submit_review", verdict)]])):
			pipeline.run_review(follow["name"])
		done = (
			db.values(
				"Code Review",
				follow["name"],
				("status", "error", "agent_turns", "outage_fallback", "posted_count"),
			)
			or {}
		)
		self.assertEqual(done["status"], "Completed", done["error"])
		self.assertEqual((done["agent_turns"], done["outage_fallback"], done["posted_count"]), (1, 0, 1))
		self.assertEqual(self.adapter.named("status")[-1], "failure")
		self.assertEqual([r["name"] for r in self.others(follow["name"])], [name])
		# One follow-up only, and none for an old commit.
		self.assertIsNone(jobs.follow_up(name))
		db.set_values("Pull Request", self.pr, {"head_sha": "0" * 40, "latest_review": name})
		self.assertIsNone(jobs.follow_up(name))

	def test_a_short_outage_or_no_fallback_waits(self):
		for minutes, after in ((5, None), (20, 0)):
			with self.subTest(minutes=minutes, after=after):
				self.outage_began(minutes)
				set_settings(outage_fallback_after=after)
				name = str(trigger.consider(self.pr, "open"))
				row = db.values("Code Review", name, ("status", "stage", "outage_fallback")) or {}
				self.assertEqual(
					(row["status"], row["stage"], row["outage_fallback"]),
					("Queued", pipeline.GATEWAY_DOWN, 0),
				)
