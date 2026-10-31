"""What a review spends: tokens by purpose, repeat reviews, pushes during a review, and backports."""

from typing import Any
from unittest.mock import PropertyMock, patch

import frappe
from frappe.tests import UnitTestCase
from frappe.utils import now_datetime

from guard.git.diff import parse_diff
from guard.llm.client import ChatRequest, LLMClient, use_responder
from guard.llm.types import ChatResult, Usage
from guard.providers.base import PullRequestInfo
from guard.review import agent, backport, jobs, pipeline, pull_requests, state, trigger
from guard.review.run import ReviewRun
from guard.tests.cases import MODELS, PullRequestCase
from guard.tests.fakes import scripted
from guard.utils import db

NOTIFY = MODELS.replace("def update_user_email(user, email):", "def update_user_email(user, email, notify):")
SUBMIT = [("submit_review", {"summary": "Looks fine.", "confidence_score": 5, "risk_level": "high"})]


class TestUsage(UnitTestCase):
	def test_usage_counts_each_purpose_and_tool_and_keeps_them_across_attempts(self):
		usage = Usage()
		usage.add(Usage(100, 20, 60, 15), "review")
		usage.add(Usage(10, 5), "verify")
		usage.add(Usage(30, 4), "review")
		usage.tool("read_file", 900)
		self.assertEqual((usage.input_tokens, usage.reasoning_tokens), (140, 15))
		self.assertEqual(
			usage.purposes["review"], {"calls": 2, "input": 130, "cached": 60, "output": 24, "reasoning": 15}
		)
		self.assertEqual(usage.purposes["verify"]["calls"], 1)
		# A retry starts from the saved detail, and adds to it.
		again = Usage.from_detail(frappe.parse_json(frappe.as_json(usage.detail())))
		again.add(Usage(1, 1), "review")
		again.tool("read_file", 100)
		self.assertEqual(again.purposes["review"]["calls"], 3)
		self.assertEqual(again.tools["read_file"], {"calls": 2, "chars": 1000})
		self.assertEqual(Usage.from_detail("not a dict").purposes, {})

	def test_the_client_counts_its_purpose_and_passes_the_cache_key(self):
		seen: list[ChatRequest] = []

		def respond(request: ChatRequest) -> ChatResult:
			seen.append(request)
			return ChatResult("ok", [], Usage(7, 3))

		usage = Usage()
		with use_responder(respond):
			LLMClient(purpose="stale", usage=usage).chat([], cache_key="k1")
			LLMClient(purpose="verify", usage=usage).chat_many([[], []], cache_keys=["a", "b"])
		self.assertEqual([r.cache_key for r in seen], ["k1", "a", "b"])
		self.assertEqual({k: v["calls"] for k, v in usage.purposes.items()}, {"stale": 1, "verify": 2})


class TestOpenAICacheKey(UnitTestCase):
	def chat(self, model: str, key: str) -> dict[str, Any]:
		from guard.llm.openai_dialect import OpenAIDialect

		bodies: list[dict[str, Any]] = []

		def post(url, body, headers, timeout):
			bodies.append(body)
			usage = {
				"prompt_tokens": 9,
				"completion_tokens": 8,
				"completion_tokens_details": {"reasoning_tokens": 6},
			}
			return {"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}], "usage": usage}

		with patch("guard.llm.openai_dialect.post_json", side_effect=post):
			result = OpenAIDialect("https://gw", "k", 30).chat(model, [], None, None, 10, 0.1, key)
		self.assertEqual(result.usage.reasoning_tokens, 6)
		return bodies[0]

	def test_openai_models_get_the_cache_key(self):
		self.assertEqual(
			self.chat("openai/gpt-6-sol", "guard-review-1-final")["prompt_cache_key"], "guard-review-1-final"
		)
		self.assertNotIn("prompt_cache_key", self.chat("openai/gpt-6-sol", ""))
		self.assertNotIn(
			"prompt_cache_key", self.chat("deepseek/deepseek-v4", "k"), "other models may refuse it"
		)


class TestBackportParsing(UnitTestCase):
	def run_of(self, title: str = "", description: str = "", branch: str = "", subjects=()) -> ReviewRun:
		run = ReviewRun(None, None, "", lambda p: None, lambda p: None, lambda p: [], "", "")
		run.title, run.description, run.head_branch = title, description, branch
		run.commits = [frappe._dict(subject=s, body="") for s in subjects]  # type: ignore[assignment]
		return run

	def test_the_numbers_of_the_originals(self):
		self.assertEqual(backport.numbers(self.run_of("fix: x (backport #62935)"), 62937), [62935])
		self.assertEqual(backport.numbers(self.run_of(description="Backport of #12 and #13"), 1), [12])
		self.assertEqual(
			backport.numbers(self.run_of(branch="mergify/bp/version-16-hotfix/pr-534"), 9), [534]
		)
		subjects = ["feat(selling): Proforma Invoice (#57263)", "Merge pull request #59892 from a/b", "wip"]
		self.assertEqual(backport.numbers(self.run_of(subjects=subjects), 1), [57263, 59892])
		self.assertEqual(backport.numbers(self.run_of("backport #5"), 5), [], "not itself")

	def test_a_file_is_covered_when_each_changed_line_is_in_an_original(self):
		def diff(lines: str) -> list:
			body = "".join(f"{line}\n" for line in lines.split("|"))
			count = len(lines.split("|"))
			return parse_diff(
				f"diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1,{count} +1,{count} @@\n{body}"
			)

		original = backport.Original(7, "", 4, "low", diff(" keep|-old()|+    new()|+more()"))
		# The backport can leave a line out.
		self.assertTrue(backport.covered(diff(" keep|-old()|+    new()")[0], [original]))
		self.assertFalse(backport.covered(diff(" keep|-old()|+other()")[0], [original]))
		# An indent changes what Python does, and the order and count of the lines count too.
		self.assertFalse(backport.covered(diff(" keep|-old()|+  new()")[0], [original]))
		self.assertFalse(backport.covered(diff(" keep|+more()|+    new()")[0], [original]))
		self.assertFalse(backport.covered(diff(" keep|+more()|+more()")[0], [original]))


class ReviewCostCase(PullRequestCase):
	def online_review(self, steps, event="open", manual=False) -> list[ChatRequest]:
		requests: list[ChatRequest] = []
		respond = scripted(steps)

		def record(request: ChatRequest) -> ChatResult:
			requests.append(request)
			return respond(request)

		with (
			patch.object(LLMClient, "online", new_callable=PropertyMock, return_value=True),
			patch("guard.config.gate.event_allowed", return_value=True),
			use_responder(record),
		):
			trigger.consider(self.pr, event, manual=manual, requested_by="ada" if manual else "")
		return requests

	def reviews(self, pull_request: str = "", *fields: str) -> list[Any]:
		return frappe.get_all(
			"Code Review",
			filters={"pull_request": pull_request or self.pr},
			fields=["name", *fields],
			order_by="creation",
		)

	def insert_review(self, **values: Any) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Code Review",
				"pull_request": self.pr,
				"repository": self.repository,
				"source": "pull_request",
				"trigger": "push",
				**values,
			}
		).insert(ignore_permissions=True)
		return str(doc.name)


class TestRepeatReviews(ReviewCostCase):
	def test_a_review_saves_its_tokens_by_purpose_and_tool(self):
		read = [("read_file", {"path": "app/models.py"})]
		with patch.object(agent, "SAME_MIN", 10):
			requests = self.online_review([read, read, SUBMIT])
		detail = frappe.parse_json(db.value("Code Review", self.reviews()[0].name, "usage_detail"))
		self.assertEqual(detail["purposes"]["review"]["calls"], 3)
		self.assertEqual(detail["tools"]["read_file"]["calls"], 2)
		self.assertEqual(detail["tools"]["submit_review"]["calls"], 1)
		outputs = [m.content for m in requests[-1].messages if m.role == "tool"]
		self.assertIn("def update_user_email", outputs[0])
		self.assertEqual(outputs[1], agent.SAME.format(name="read_file", turn=1), "the same read points up")
		self.assertEqual(
			requests[0].cache_key, f"guard-review-{self.reviews()[0].name}-final", "one cache for the agent"
		)

	def test_the_wait_of_a_push_review_grows_with_recent_reviews(self):
		frappe.db.set_single_value("Guard Settings", "push_review_delay", 120)
		self.addCleanup(frappe.db.set_single_value, "Guard Settings", "push_review_delay", 120)
		self.assertEqual(trigger.push_delay("push", False, self.pr), 120)
		# A review that a newer push replaced while it waited cost nothing, and does not count.
		self.insert_review(status="Cancelled")
		for _ in range(2):
			self.insert_review(status="Completed", started_at=now_datetime())
		self.assertEqual(trigger.push_delay("push", False, self.pr), 480)
		for _ in range(5):
			self.insert_review(status="Completed", started_at=now_datetime())
		self.assertEqual(trigger.push_delay("push", False, self.pr), trigger.MAX_PUSH_DELAY)
		self.assertEqual(trigger.push_delay("push", True, self.pr), 0, "a request does not wait")

	def test_a_pull_request_over_its_daily_tokens_gets_no_push_review(self):
		frappe.db.set_single_value("Guard Settings", "pull_request_daily_tokens", 1000)
		self.addCleanup(frappe.db.set_single_value, "Guard Settings", "pull_request_daily_tokens", 10_000_000)
		self.insert_review(status="Completed", input_tokens=1000)
		with patch("guard.config.gate.event_allowed", return_value=True):
			self.assertIsNone(trigger.consider(self.pr, "push"))
		self.assertIn(trigger.OVER_BUDGET, self.adapter.checks[-1][2])
		# A person can still ask.
		self.assertTrue(self.online_review([SUBMIT], event="push", manual=True))

	def test_a_repository_over_its_daily_reviews_gets_no_review(self):
		from guard.api import ui

		frappe.db.set_single_value("Guard Settings", "repository_daily_reviews", 2)
		self.addCleanup(frappe.db.set_single_value, "Guard Settings", "repository_daily_reviews", 0)
		# Not counted: replaced before it started, a local review, a review in an outage, and a queued
		# review of this pull request, which the new review replaces.
		self.insert_review(status="Cancelled")
		self.insert_review(status="Completed", source="local", started_at=now_datetime())
		self.insert_review(status="Completed", outage_fallback=1, started_at=now_datetime())
		queued = self.insert_review(status="Queued")
		self.insert_review(status="Completed", started_at=now_datetime())
		self.assertFalse(trigger.over_repository_limit(self.repository, self.pr))
		self.insert_review(status="Completed", started_at=now_datetime())
		self.assertTrue(trigger.over_repository_limit(self.repository, self.pr))
		# A queued review owns the check, and a skip would not replace it.
		frappe.db.set_value("Code Review", queued, "status", "Cancelled")
		with patch("guard.config.gate.event_allowed", return_value=True):
			self.assertIsNone(trigger.consider(self.pr, "push"))
			# A person cannot ask either.
			self.assertIsNone(trigger.consider(self.pr, "open", manual=True, requested_by="ada"))
		self.assertIn(trigger.OVER_REPOSITORY_LIMIT, self.adapter.checks[-1][2])
		self.assertRaises(frappe.ValidationError, ui.rereview, self.pr)
		frappe.db.set_single_value("Guard Settings", "repository_daily_reviews", 0)
		self.assertFalse(trigger.over_repository_limit(self.repository, self.pr), "0 is no limit")

	def test_a_rereview_of_a_small_push_gets_few_turns(self):
		self.online_review([SUBMIT])
		self.repo.commit({"app/extra.py": "VALUE = 1\n"}, "push")
		frappe.db.set_value("Pull Request", self.pr, "head_sha", self.repo.head())
		reads = [[("read_file", {"path": "app/models.py", "start_line": n})] for n in range(1, 20)]
		self.online_review(reads, event="push")
		row = db.values("Code Review", self.reviews()[-1].name, ("agent_turns", "agent_round_limit")) or {}
		self.assertEqual((row["agent_turns"], row["agent_round_limit"]), (pipeline.DELTA_TURNS,) * 2)

	def test_a_push_lets_a_review_that_is_nearly_done_finish(self):
		frappe.db.set_single_value("Guard Settings", "push_review_delay", 0)
		self.addCleanup(frappe.db.set_single_value, "Guard Settings", "push_review_delay", 120)
		finishing = self.insert_review(status="Running", stage="verify", tier="plus")
		early = self.insert_review(status="Running", stage="review", tier="plus", agent_rounds=1)
		# A re-review of a push has fewer rounds than its tier: 3 of 5 is past half.
		delta = self.insert_review(
			status="Running", stage="review", tier="plus", agent_rounds=3, agent_round_limit=5
		)
		real, later = jobs.enqueue_review, []

		def enqueue(review, delay=0, attempt=0, tier=""):
			# Tests run a job at once, also one with a delay: keep the wait from running again.
			if delay or attempt:
				later.append((review, delay))
				return
			real(review, delay=delay, attempt=attempt, tier=tier)

		with (
			patch("guard.config.gate.event_allowed", return_value=True),
			patch("guard.review.jobs.enqueue_review", side_effect=enqueue),
		):
			newer = trigger.consider(self.pr, "push")
		self.assertEqual(later, [(newer, pipeline.SLOT_WAIT)])
		self.assertEqual(db.value("Code Review", finishing, "status"), "Running")
		self.assertEqual(db.value("Code Review", early, "status"), "Cancelled")
		self.assertEqual(db.value("Code Review", delta, "status"), "Running")
		# The review of the push waits for it, and then reads only the changes since.
		row = db.values("Code Review", str(newer), ("status", "stage")) or {}
		self.assertEqual((row["status"], row["stage"]), ("Queued", jobs.WAITING_FOR_LAST))
		state.move(finishing, {"status": "Completed"})
		state.move(delta, {"status": "Completed"})
		state.move(str(newer), {"status": "Cancelled"})


class TestBackportReview(ReviewCostCase):
	def backport(
		self, extra: dict[str, str] | None = None, manual: bool = False
	) -> tuple[str, list[ChatRequest]]:
		"""Review #7 on main, then open #8 to `release` with the same change, and review it."""
		self.online_review([SUBMIT])
		frappe.db.set_value("Pull Request", self.pr, "state", "merged")
		self.repo.git("checkout", "-q", "-b", "release", self.base)
		self.repo.commit({"app/__init__.py": "VERSION = 1\n"}, "release")
		base = self.repo.head()
		self.repo.branch_off("bp")
		self.repo.commit({"app/models.py": NOTIFY, **(extra or {})}, "add notify (#7)")
		info = PullRequestInfo(
			8, "Add notify flag", "", "ada", "open", False, "release", "bp", base, self.repo.head(), "u"
		)
		self.adapter.info, self.adapter.branch = info, "bp"
		self.pr = str(pull_requests.upsert(self.repository, info).name)
		return self.pr, self.online_review([SUBMIT], manual=manual)

	def test_a_backport_of_reviewed_changes_runs_no_agent(self):
		name, requests = self.backport()
		self.assertEqual(db.value("Pull Request", name, "backport_of"), "#7")
		self.assertFalse([r for r in requests if r.purpose == "review"])
		row = (
			db.values(
				"Code Review", self.reviews(name)[0].name, ("status", "summary", "risk_level", "agent_turns")
			)
			or {}
		)
		self.assertEqual((row["status"], row["risk_level"], row["agent_turns"]), ("Completed", "high", 0))
		self.assertIn("backports #7", row["summary"])

	def test_a_backport_reviews_only_the_files_that_differ(self):
		_, requests = self.backport({"app/extra.py": "VALUE = 1\n"})
		task = next(r for r in requests if r.purpose == "review").messages[1].content
		self.assertIn("This pull request backports #7", task)
		self.assertIn("Annotated diff (files that differ from #7)", task)
		self.assertIn("### app/extra.py", task)
		self.assertNotIn("### app/models.py", task)
		self.assertIn("with the same changes as the originals:\n- app/models.py", task)

	def test_a_backport_can_be_reviewed_in_full(self):
		with patch.dict("guard.config.schema.DEFAULTS", {"reuse_backport_reviews": False}):
			name, requests = self.backport()
		self.assertEqual(db.value("Pull Request", name, "backport_of"), "#7")
		task = next(r for r in requests if r.purpose == "review").messages[1].content
		self.assertIn("### app/models.py", task)

	def test_a_person_who_asks_gets_a_full_review_of_a_backport(self):
		_, requests = self.backport(manual=True)
		task = next(r for r in requests if r.purpose == "review").messages[1].content
		self.assertIn("### app/models.py", task)
		self.assertNotIn("backports #7", task)
