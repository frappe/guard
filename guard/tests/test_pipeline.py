import time
from types import SimpleNamespace
from typing import Any
from unittest.mock import PropertyMock, patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from guard.git.diff import parse_diff
from guard.graph.overlay import ChangedEntity
from guard.graph.queries import Link
from guard.llm.client import ChatRequest, LLMClient, use_responder
from guard.llm.types import ChatResult, GatewayBilling, GatewayBusy, Message, ToolCall, Usage
from guard.providers.base import ProviderComment, ProviderError
from guard.review import jobs, state, trigger, verify
from guard.review import pipeline as pipeline_module
from guard.review.agent import Agent
from guard.review.context import BuiltContext
from guard.review.findings import Finding
from guard.review.pipeline import ReviewPipeline
from guard.review.prompt import ContextFile, ContextPack
from guard.review.run import ReviewRun, Verdict
from guard.settings import GatewayConfig
from guard.tests.cases import AUTO_APPROVE, PullRequestCase
from guard.tests.fakes import dsml, scripted
from guard.utils import db

PROBLEM = {
	"path": "app/models.py",
	"start_line": 5,
	"severity": "P2",
	"comment_type": "style",
	"title": "Name is vague",
	"body": "x",
	"confidence": 0.9,
}
MISSED_NOTIFY: dict[str, Any] = {
	"path": "app/models.py",
	"start_line": 5,
	"severity": "P1",
	"comment_type": "logic",
	"title": "Callers of update_user_email miss the notify argument",
	"body": "`app/service.py:6` still calls it with two arguments.",
}


class TestPipeline(PullRequestCase):
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
		self.assertEqual(review["credits"], 3)  # a first review is at least plus
		timing = db.values("Code Review", str(name), ("agent_turns", "agent_rounds", "stage_seconds")) or {}
		self.assertEqual((timing["agent_turns"], timing["agent_rounds"]), (2, 2))  # one agent: a turn a round
		stages = frappe.parse_json(timing["stage_seconds"])
		self.assertLessEqual({"prepare", "index", "review", "publish", "after"}, set(stages))
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
		self.assertEqual(self.adapter.named("status"), ["queued", "pending", "failure"])
		self.assertIn("+1", self.adapter.named("react"))

	def test_summary_is_posted_above_the_inline_comments(self):
		self.review(
			[[("report_finding", PROBLEM), ("submit_review", {"summary": "s", "confidence_score": 4})]]
		)
		kinds = [
			kind
			for kind, _ in self.adapter.calls
			if kind in ("post_comment", "create_review", "update_comment")
		]
		self.assertEqual(kinds, ["post_comment", "create_review", "update_comment"])
		# The edit adds the link to the inline comment.
		summary_id = self.adapter.named("update_comment")[0]
		inline_url = frappe.db.get_value("Review Finding", {"pull_request": self.pr}, "comment_url")
		self.assertNotIn(inline_url, self.adapter.named("post_comment")[0])
		self.assertIn(inline_url, self.adapter.comments[summary_id])

	def test_review_waits_while_the_index_is_busy(self):
		from guard.indexing.indexer import index_lock
		from guard.indexing.jobs import index_for
		from guard.review import jobs

		real, retries = jobs.enqueue_review, []

		def enqueue(review, delay=0, attempt=0, tier=""):
			if attempt:
				retries.append((delay, attempt))
				return
			real(review, delay=delay, attempt=attempt, tier=tier)

		with (
			index_lock(index_for(self.repository)),
			patch("guard.review.jobs.enqueue_review", side_effect=enqueue),
		):
			name = self.review([])
		row = db.values("Code Review", str(name), ("status", "stage")) or {}
		self.assertEqual((row["status"], row["stage"]), ("Queued", "waiting 60s for the code graph index"))
		self.assertEqual(retries, [(60, 1)])

	def test_a_failed_attempt_keeps_its_tokens_and_the_next_attempt_adds_to_them(self):
		submit: list[list[tuple[str, dict[str, Any]]] | str] = [
			[("submit_review", {"summary": "Fine.", "confidence_score": 5})]
		]
		with (
			patch("guard.review.verify.verify", side_effect=RuntimeError("verifier broke")),
			self.assertRaises(RuntimeError),
		):
			self.review(submit)
		name = str(frappe.get_last_doc("Code Review", filters={"pull_request": self.pr}).name)
		failed = db.values("Code Review", name, ("status", "input_tokens", "agent_turns")) or {}
		self.assertEqual(failed["status"], "Failed")
		self.assertEqual(failed["agent_turns"], 1)
		self.assertGreaterEqual(failed["input_tokens"], 100)
		# As a retry does: only a queued review starts again.
		db.set_values("Code Review", name, {"status": "Queued"})
		with use_responder(scripted(submit)):
			ReviewPipeline(name).attempt(1)
		done = db.values("Code Review", name, ("status", "input_tokens", "agent_turns")) or {}
		self.assertEqual((done["status"], done["agent_turns"]), ("Completed", 2))
		self.assertGreaterEqual(done["input_tokens"], failed["input_tokens"] + 100)

	def test_a_failed_review_logs_its_traceback(self):
		# Error Logs outlive the test transaction, and review names repeat across tests.
		earlier = set(frappe.get_all("Error Log", filters={"reference_doctype": "Code Review"}, pluck="name"))
		with (
			patch("guard.review.verify.verify", side_effect=RuntimeError("verifier broke")),
			self.assertRaises(RuntimeError),
		):
			self.review([[("submit_review", {"summary": "Fine.", "confidence_score": 5})]])
		name = str(frappe.get_last_doc("Code Review", filters={"pull_request": self.pr}).name)
		logs = [
			frappe.get_doc("Error Log", log)
			for log in frappe.get_all("Error Log", filters={"reference_doctype": "Code Review"}, pluck="name")
			if log not in earlier
		]
		self.assertEqual([log.get("reference_name") for log in logs], [name])
		self.assertIn("Traceback", str(logs[0].get("error")))
		self.assertIn("verifier broke", str(logs[0].get("error")))

	def test_a_review_writes_the_pull_request_before_the_code_review(self):
		"""The webhook handler locks the Pull Request row, then the Code Review row. The other order deadlocks."""
		from frappe.model.document import Document

		real_set, real_commit = Document.db_set, db.commit
		transactions: list[list[str]] = [[]]

		def db_set(doc, *args, **kwargs):
			transactions[-1].append(doc.doctype)
			return real_set(doc, *args, **kwargs)

		def commit():
			transactions.append([])
			real_commit()

		# Tests run jobs inline. In production after_review runs on its own, after the commit.
		with (
			patch.object(Document, "db_set", db_set),
			patch.object(db, "commit", commit),
			patch("guard.review.jobs.after_review"),
		):
			name = self.review([[("submit_review", {"summary": "Fine.", "confidence_score": 5})]])
		self.assertEqual(self.row(name)["status"], "Completed")
		both = [w for w in transactions if "Code Review" in w and "Pull Request" in w]
		self.assertTrue(both)
		for writes in both:
			self.assertNotIn("Pull Request", writes[writes.index("Code Review") :], writes)

	def test_a_busy_gateway_gets_longer_waits_than_other_retries(self):
		name = self.running_review("review", attempt=3)
		pipeline = ReviewPipeline(name)
		with patch("guard.review.jobs.enqueue_review") as enqueue:
			pipeline.retry(3, 60, "the AI gateway", "busy", pipeline_module.BUSY_DELAYS)
		self.assertEqual(db.value("Code Review", name, "status"), "Queued")
		enqueue.assert_called_once_with(name, delay=2400, attempt=4)
		with patch("guard.review.jobs.enqueue_review") as enqueue:
			pipeline.retry(3, 60, "the code graph index", "index busy")
		self.assertEqual(db.value("Code Review", name, "status"), "Failed")

	def test_review_waits_while_the_gateway_is_down(self):
		from guard.llm.types import GatewayDown
		from guard.review import jobs

		real, retries = jobs.enqueue_review, []

		def enqueue(review, delay=0, attempt=0, tier=""):
			if attempt:
				retries.append((delay, attempt))
				return
			real(review, delay=delay, attempt=attempt, tier=tier)

		def down(request):
			raise GatewayDown("Gateway request failed: Gateway answered 502: upstream account unpaid")

		with (
			use_responder(down),
			patch("guard.llm.client.LLMClient.online", new=True),
			patch("guard.review.jobs.enqueue_review", side_effect=enqueue),
		):
			name = trigger.consider(self.pr, "open")
		row = db.values("Code Review", str(name), ("status", "stage")) or {}
		self.assertEqual((row["status"], row["stage"]), ("Queued", "waiting 60s for the AI gateway"))
		self.assertEqual(retries, [(60, 1)])

	def test_review_goes_on_without_a_failed_index(self):
		from guard.indexing.indexer import Indexer

		with patch.object(Indexer, "_run", side_effect=RuntimeError("parser broke")) as index:
			first = self.review([])
			with use_responder(scripted([])):
				second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		for name in (first, second):
			self.assertEqual(self.row(name)["status"], "Completed", self.row(name)["error"])
		# The second review skips the index that failed a moment ago, instead of failing again.
		self.assertEqual(index.call_count, 1)
		summary = self.adapter.named("post_comment")[0]
		self.assertIn("The code graph is not available", summary)
		self.assertIn("parser broke", summary)

	def test_review_waits_for_a_free_slot(self):
		from frappe.utils import now_datetime

		from guard.review import jobs

		real, later = jobs.enqueue_review, []

		def enqueue(review, delay=0, attempt=0, tier=""):
			if delay or attempt:
				later.append((review, delay, attempt))
				return
			real(review, delay=delay, attempt=attempt, tier=tier)

		frappe.get_doc(
			{
				"doctype": "Code Review",
				"pull_request": self.pr,
				"repository": self.repository,
				"source": "pull_request",
				"status": "Running",
				"trigger": "open",
				"started_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
		frappe.db.set_single_value("Guard Settings", "max_concurrent_reviews", 1)
		self.addCleanup(frappe.db.set_single_value, "Guard Settings", "max_concurrent_reviews", 4)
		with patch("guard.review.jobs.enqueue_review", side_effect=enqueue):
			waiting = trigger.consider(self.pr, "open")
		row = db.values("Code Review", str(waiting), ("status", "stage")) or {}
		self.assertEqual((row["status"], row["stage"]), ("Queued", "waiting for a free review slot"))
		self.assertEqual(later, [(waiting, 60, 0)])  # the retries stay unused

		frappe.db.set_single_value("Guard Settings", "max_concurrent_reviews", 0)  # no limit
		self.assertEqual(self.row(self.review([]))["status"], "Completed")

	def test_bulk_retry_makes_one_review_for_each_pull_request(self):
		from guard.api.ui import retry_reviews

		done = self.review([])
		failed = []
		for status in ("Failed", "Cancelled"):
			doc = frappe.get_doc(
				{
					"doctype": "Code Review",
					"pull_request": self.pr,
					"repository": self.repository,
					"source": "pull_request",
					"status": status,
					"trigger": "open",
				}
			).insert(ignore_permissions=True)
			failed.append(str(doc.name))
		with use_responder(scripted([])):
			created = retry_reviews([*failed, str(done)])
		self.assertEqual(len(created), 1)  # two failed reviews of one pull request, and a finished one
		self.assertNotIn(created[0], [*failed, str(done)])
		self.assertEqual(self.row(created[0])["status"], "Completed")

	def test_health_status_and_failure_alert(self):
		from frappe.utils import now_datetime

		from guard.api import health
		from guard.utils import cache

		cache.delete("guard:notified:failure-rate")
		for _ in range(health.ALERT_MIN + 1):
			frappe.get_doc(
				{
					"doctype": "Code Review",
					"pull_request": self.pr,
					"repository": self.repository,
					"source": "pull_request",
					"status": "Failed",
					"trigger": "open",
					"error": "LLMError: Gateway answered 404",
					"completed_at": now_datetime(),
				}
			).insert(ignore_permissions=True)
		data = health.status()
		self.assertEqual([q["queue"] for q in data["queues"]], ["short", "default", "long"])
		self.assertGreaterEqual(data["reviews"].get("Failed", 0), health.ALERT_MIN + 1)
		self.assertIn("LLMError: Gateway answered 404", [f["cause"] for f in data["failures"]])
		self.assertGreater(data["failure_rate"]["rate"], health.ALERT_RATE)
		self.assertGreaterEqual(data["metrics"]["summary"]["failed"], health.ALERT_MIN + 1)
		self.assertEqual(len(data["metrics"]["series"]), 24)
		self.assertEqual(len(health.status(hours=7 * 24)["metrics"]["series"]), 7)
		self.assertEqual(len(health.status(hours=1)["metrics"]["series"]), 12)

		target = "frappe.desk.doctype.notification_log.notification_log.enqueue_create_notification"
		with (
			patch(target) as send,
			patch("frappe.utils.user.get_users_with_role", return_value=["ops@example.com"]),
		):
			health.check_failure_rate()
			health.check_failure_rate()  # within the hour: no second notification
		send.assert_called_once()
		self.assertIn("reviews failed in the last hour", send.call_args.args[1]["subject"])

	def test_eval_case_from_a_finished_review(self):
		from guard.evals import build, run

		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers of update_user_email miss the notify argument",
			"body": "`app/service.py:6` still calls it with two arguments.",
			"confidence": 0.9,
		}
		rejected = {**finding, "start_line": 1, "severity": "P2", "title": "Rename the signature flag"}
		name = self.review(
			[
				[
					("report_finding", finding),
					("report_finding", rejected),
					("submit_review", {"summary": "s", "confidence_score": 3}),
				]
			]
		)
		for title, status in ((finding["title"], "Addressed"), (rejected["title"], "Ignored")):
			frappe.db.set_value("Review Finding", {"code_review": name, "title": title}, "status", status)
		case = build.case_from_review(str(name))
		assert case is not None
		self.assertIn("app/models.py", case["payload"]["files"])
		self.assertIn("def update_user_email(user, email, notify):", case["payload"]["diff"])
		self.assertEqual(case["expected"][0]["keywords"], ["update_user_email", "argument"])
		self.assertEqual(case["unexpected"][0]["keywords"], ["signature", "rename"])

		# Replayed, the same findings come back: the addressed one is found, the ignored one raised again.
		with use_responder(
			scripted(
				[
					[
						("report_finding", finding),
						("report_finding", rejected),
						("submit_review", {"summary": "s", "confidence_score": 3}),
					]
				]
			)
		):
			[result] = [run.review_case(case)]
		summary = run.score([result])
		self.assertEqual((result["found"], result["expected"]), (1, 1))
		self.assertEqual(result["raised"], [rejected["title"]])
		self.assertEqual(summary["recall"], 1.0)
		self.assertIn("| 1/1 | 1/1 |", summary["markdown"])

	def test_an_eval_review_that_parks_runs_again(self):
		from guard.evals import build, run

		calls: list[str] = []
		answer = scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])

		def busy_once(request: ChatRequest) -> ChatResult:
			calls.append(request.purpose)
			if len(calls) == 1:
				raise GatewayBusy("429", 5)
			return answer(request)

		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers of update_user_email miss the notify argument",
			"body": "`app/service.py:6` still calls it with two arguments.",
		}
		name = self.review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 3})]]
		)
		frappe.db.set_value("Review Finding", {"code_review": name}, "status", "Addressed")
		case = build.case_from_review(str(name))
		assert case is not None
		# As on a site with a scheduler: a retry only marks the review, it does not run it.
		with (
			use_responder(busy_once),
			patch("guard.review.jobs.enqueue_review"),
			patch("guard.evals.run.time.sleep") as sleep,
		):
			result = run.review_case(case)
		self.assertEqual(result["status"], "Completed")
		sleep.assert_called_once_with(run.PARK_WAIT)

	def test_an_eval_stops_at_once_when_the_gateway_refuses_the_key_for_money(self):
		from guard.evals import build, run

		def unpaid(request: ChatRequest) -> ChatResult:
			raise GatewayBilling("Gateway answered 402: credit balance exhausted", 300)

		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers of update_user_email miss the notify argument",
			"body": "`app/service.py:6` still calls it with two arguments.",
		}
		name = self.review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 3})]]
		)
		frappe.db.set_value("Review Finding", {"code_review": name}, "status", "Addressed")
		case = build.case_from_review(str(name))
		assert case is not None
		with (
			use_responder(unpaid),
			patch("guard.llm.client.LLMClient.online", new=True),
			patch("guard.review.jobs.enqueue_review"),
			patch("guard.evals.run.time.sleep") as sleep,
		):
			result = run.review_case(case)
		sleep.assert_not_called()
		self.assertEqual((result["status"], result["billing"]), ("Failed", True))
		self.assertIn("credit balance exhausted", result["error"])
		# The later cases do not run: they would stop at their first model call too.
		with (
			patch("guard.evals.run.load_cases", return_value=[case, case]),
			patch("guard.evals.run.review_case", return_value=result) as review_case,
		):
			run.run()
		review_case.assert_called_once()

	def test_breaker_pauses_reviews_and_reruns_them(self):
		from frappe.utils import now_datetime

		from guard.llm import breaker
		from guard.review import jobs

		real, later = jobs.enqueue_review, []

		def enqueue(review, delay=0, attempt=0, tier=""):
			if delay or attempt:
				later.append((review, attempt))
				return
			real(review, delay=delay, attempt=attempt, tier=tier)

		with patch("guard.llm.breaker._notify_admins") as notify:
			for _ in range(breaker.THRESHOLD + 1):
				breaker.record_failure("Gateway answered 502: upstream account unpaid")
		self.assertGreater(breaker.wait_seconds(), 0)
		notify.assert_called_once()

		# While the breaker is open, a review waits and keeps its retries.
		with patch("guard.review.jobs.enqueue_review", side_effect=enqueue):
			paused = trigger.consider(self.pr, "open")
		row = db.values("Code Review", str(paused), ("status", "stage")) or {}
		self.assertEqual((row["status"], row["stage"]), ("Queued", "paused: the AI gateway is down"))
		self.assertEqual(later, [(paused, 0)])

		# A review that failed in the outage runs again when the gateway answers.
		failed = frappe.get_doc(
			{
				"doctype": "Code Review",
				"pull_request": self.pr,
				"repository": self.repository,
				"source": "pull_request",
				"status": "Failed",
				"trigger": "open",
				"error": f"{breaker.FAILED_PREFIX}: Gateway answered 502",
				"completed_at": now_datetime(),
			}
		).insert(ignore_permissions=True)
		later.clear()
		with patch(
			"guard.review.jobs.enqueue_review", side_effect=lambda review, **kw: later.append((review, 0))
		):
			breaker.record_success()
		self.assertEqual(breaker.wait_seconds(), 0)
		self.assertEqual(later, [(str(failed.name), 0)])
		self.assertEqual(db.value("Code Review", str(failed.name), "status"), "Queued")

	def test_dead_review_job_fails_the_review(self):
		from guard.review.jobs import fail_dead_reviews

		# What a worker restart leaves: Running, and no job in the queue. It had started to post.
		dead = self.running_review("publish")
		with patch("guard.review.jobs.is_job_enqueued", return_value=True):
			fail_dead_reviews()
		self.assertEqual(db.value("Code Review", dead, "status"), "Running")
		fail_dead_reviews()
		self.assertEqual(db.value("Code Review", dead, "status"), "Failed")
		self.assertEqual(self.adapter.named("status"), ["error"])
		self.assertIn("confused", self.adapter.named("react"))

	def test_failed_review_closes_the_check_when_the_reaction_fails(self):
		from guard.review.jobs import fail_dead_reviews

		dead = self.running_review("publish")
		with patch.object(self.adapter, "add_reaction", side_effect=ProviderError("no reactions")):
			fail_dead_reviews()
		self.assertEqual(db.value("Code Review", dead, "status"), "Failed")
		self.assertEqual(self.adapter.named("status"), ["error"])

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
		self.adapter.calls.clear()
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
		self.assertFalse(self.adapter.named("post_comment"))
		# Raised again, so it still applies: the thread stays open.
		self.assertFalse(self.adapter.named("resolve"))

	def test_rereview_does_not_post_a_finding_that_a_person_dismissed(self):
		from guard.api import ui

		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P1",
			"comment_type": "logic",
			"title": "Callers are not updated",
			"body": "x",
			"confidence": 0.9,
		}
		first = self.review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 4})]]
		)
		[earlier] = frappe.get_all("Review Finding", filters={"code_review": first}, pluck="name")
		ui.set_finding_status(earlier, "Dismissed")
		self.adapter.calls.clear()
		with use_responder(
			scripted(
				[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 4})]]
			)
		):
			second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		review = self.row(second)
		self.assertEqual(review["status"], "Completed", review["error"])
		self.assertEqual(review["posted_count"], 0)
		self.assertFalse(self.adapter.named("unresolve"))
		self.assertEqual(db.value("Review Finding", earlier, "status"), "Dismissed")
		self.assertEqual(
			frappe.get_all("Review Finding", filters={"code_review": second}, pluck="status_reason"),
			["dismissed earlier on this pull request"],
		)

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
		models = (self.repo.path / "app/models.py").read_text() + "\n\nFIXED = 1\n"
		self.repo.commit({"app/models.py": models}, "fix callers")
		frappe.db.set_value("Pull Request", self.pr, "head_sha", self.repo.head())
		requests = self.online_review(
			[[("submit_review", {"summary": "s", "confidence_score": 5})]],
			{"stale": '{"state": "fixed"}'},
			manual=True,
		)
		self.assertEqual(
			frappe.get_all("Review Finding", filters={"pull_request": self.pr}, pluck="status"), ["Addressed"]
		)
		self.assertEqual(len(self.adapter.named("resolve")), 1)
		self.assertEqual(self.adapter.named("status")[-1], "success")
		[stale] = [r for r in requests if r.purpose == "stale"]
		self.assertIn("FIXED = 1", stale.messages[-1].content, "the model reads the changes since")

	def test_rereview_of_the_same_head_asks_no_stale_model(self):
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
		requests = self.online_review(
			[[("submit_review", {"summary": "s", "confidence_score": 5})]],
			{"stale": '{"state": "fixed"}'},
			manual=True,
		)
		self.assertFalse([r for r in requests if r.purpose == "stale"])
		self.assertEqual(
			frappe.get_all("Review Finding", filters={"pull_request": self.pr}, pluck="status"), ["Open"]
		)

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
		# erpnext #62627: a held-back P2 kept the score at 4, and the check failed with no findings.
		self.assertEqual(self.row(second)["confidence_score"], 5)
		self.assertEqual(self.adapter.checks[-1][1:3], ("success", "5/5 · 0 open finding(s)"))

	def test_score_of_a_refuted_finding_is_fact_checked(self):
		# erpnext #62149: every finding was refuted or held back, and the score of 3 stayed.
		finding = dict(PROBLEM, severity="P1")
		refuted = '[{"index": 0, "valid": false, "reason": "the caller passes notify"}]'
		checked = '{"score": 5, "reason": "The only issue did not hold up."}'
		verdict = {"summary": "s", "confidence_score": 3, "confidence_reason": "Callers break."}
		requests = self.online_review(
			[[("report_finding", finding), ("submit_review", verdict)]],
			{"verify": refuted, "verdict": checked},
		)
		review = db.values(
			"Code Review", {"pull_request": self.pr}, ("confidence_score", "confidence_reason")
		)
		self.assertEqual(
			review, {"confidence_score": 5, "confidence_reason": "The only issue did not hold up."}
		)
		[check] = [r for r in requests if r.purpose == "verdict"]
		self.assertIn("refuted: the caller passes notify", check.messages[-1].content)
		self.assertIn("Reason: Callers break.", check.messages[-1].content)

	def test_held_back_p2_points_alone_score_5_without_a_fact_check(self):
		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		verdict = {"summary": "s", "confidence_score": 4, "confidence_reason": "A naming point is left."}
		requests = self.online_review(
			[[("report_finding", PROBLEM), ("submit_review", verdict)]],
			{"verify": '[{"index": 0, "valid": true}]', "verdict": '{"score": 4, "reason": "r"}'},
			manual=True,
		)
		latest = frappe.get_all(
			"Code Review",
			filters={"pull_request": self.pr},
			fields=["confidence_score", "confidence_reason"],
			order_by="creation desc",
		)[0]
		self.assertEqual((latest.confidence_score, latest.confidence_reason), (5, verify.MINOR_REASON))
		self.assertFalse([r for r in requests if r.purpose == "verdict"])

	def test_no_fact_check_when_posted_findings_back_the_score(self):
		finding = dict(PROBLEM, severity="P1")
		requests = self.online_review(
			[[("report_finding", finding), ("submit_review", {"summary": "s", "confidence_score": 3})]],
			{"verify": '[{"index": 0, "valid": true}]', "verdict": '{"score": 5}'},
		)
		self.assertFalse([r for r in requests if r.purpose == "verdict"])
		self.assertEqual(db.value("Code Review", {"pull_request": self.pr}, "confidence_score"), 3)

	def test_dead_review_job_starts_again_before_it_posts(self):
		from guard.review import jobs

		dead, worn = self.running_review("review"), self.running_review("review", jobs.MAX_ATTEMPT - 1)
		with patch("guard.review.jobs.enqueue_review") as enqueue:
			jobs.fail_dead_reviews()
		enqueue.assert_called_once_with(dead, attempt=1, tier="")
		row = db.values("Code Review", dead, ("status", "stage"))
		self.assertEqual(row, {"status": "Queued", "stage": jobs.RESTARTED})
		# One that stopped too often fails, so it cannot loop for ever.
		self.assertEqual(db.value("Code Review", worn, "status"), "Failed")

	def test_reason_is_fact_checked_when_findings_were_dropped(self):
		# erpnext #62149: one P1 posted, one refuted, and the reason said "several real gaps".
		posted = dict(PROBLEM, severity="P1", title="Saving old bundles breaks")
		refuted = dict(PROBLEM, severity="P1", start_line=6, title="Rows are skipped")
		verdict = {"summary": "s", "confidence_score": 3, "confidence_reason": "Several real gaps remain."}
		answers = {
			"verify": '[{"index": 0, "valid": true}, {"index": 1, "valid": false, "reason": "no"}]',
			"verdict": '{"score": 3, "reason": "One issue is left: saving old bundles breaks."}',
		}
		requests = self.online_review(
			[[("report_finding", posted), ("report_finding", refuted), ("submit_review", verdict)]], answers
		)
		review = db.values(
			"Code Review", {"pull_request": self.pr}, ("confidence_score", "confidence_reason")
		)
		self.assertEqual(
			review,
			{"confidence_score": 3, "confidence_reason": "One issue is left: saving old bundles breaks."},
		)
		self.assertEqual(len([r for r in requests if r.purpose == "verdict"]), 1)

	def test_fact_check_sees_earlier_findings_that_still_apply(self):
		earlier = dict(PROBLEM, severity="P1", title="Saving old bundles breaks")
		self.review(
			[[("report_finding", earlier), ("submit_review", {"summary": "s", "confidence_score": 3})]]
		)
		clean = {"summary": "s", "confidence_score": 5, "confidence_reason": "Ready to merge."}
		checked = '{"score": 3, "reason": "An earlier P1 still applies: saving old bundles breaks."}'
		requests = self.online_review(
			[[("submit_review", clean)]], {"stale": '{"state": "applies"}', "verdict": checked}, manual=True
		)
		[check] = [r for r in requests if r.purpose == "verdict"]
		self.assertIn("open from an earlier review: it still applies", check.messages[-1].content)
		self.assertIn("Saving old bundles breaks", check.messages[-1].content)
		latest = frappe.get_all(
			"Code Review",
			filters={"pull_request": self.pr},
			fields=["confidence_score", "confidence_reason"],
			order_by="creation desc",
		)[0]
		self.assertEqual(latest.confidence_score, 3)
		self.assertEqual(latest.confidence_reason, "An earlier P1 still applies: saving old bundles breaks.")

	def test_cancelled_review_closes_its_check(self):
		# A newer review cancels this one while the model runs. Its check said "Guard is reviewing".
		def cancel_then_submit(request):
			name = frappe.get_all("Code Review", filters={"pull_request": self.pr}, pluck="name")[0]
			state.cancel(name, state.REPLACED)
			return respond(request)

		respond = scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		with use_responder(cancel_then_submit):
			name = trigger.consider(self.pr, "open")
		self.assertEqual(self.row(name)["status"], "Cancelled")
		check = db.value("Code Review", str(name), "check_run_id")
		self.assertEqual(
			self.adapter.checks[-1],
			(self.head, "cancelled", "Guard stopped this review: a newer review replaced it.", check),
		)
		self.assertNotIn("+1", self.adapter.named("react"))

	def test_older_job_of_a_review_stops_when_a_newer_job_takes_it(self):
		# suite #881: after a deploy, a second job ran the review while the first one still ran.
		def take_over_then_submit(request):
			name = frappe.get_all("Code Review", filters={"pull_request": self.pr}, pluck="name")[0]
			db.set_values("Code Review", name, {"job_id": "newer-job", "input_tokens": 12345})
			return respond(request)

		respond = scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		with use_responder(take_over_then_submit):
			name = str(trigger.consider(self.pr, "open"))
		# Rows outlive the test: fail_dead_reviews in a later test would start this one again.
		self.addCleanup(db.set_values, "Code Review", name, {"status": "Completed"})
		# The newer job owns the status, the usage and the check. The older one wrote nothing more.
		row = db.values("Code Review", name, ("status", "stage", "input_tokens")) or {}
		self.assertEqual((row["status"], row["input_tokens"]), ("Running", 12345))
		self.assertNotIn(row["stage"], ("publish", "after"))
		self.assertEqual(self.adapter.named("close_checks"), [])
		self.assertEqual(self.adapter.named("status"), ["queued", "pending"])

	def test_hourly_job_closes_checks_of_finished_commits_only(self):
		from guard.review import jobs

		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		# Other tests' repositories can have the same commit: look for this repository's one.
		commit = (str(db.value("Code Repository", self.repository, "full_name")), self.head)
		with patch("guard.providers.adapter_for_repository", return_value=self.adapter):
			jobs.close_stuck_checks()
			self.assertIn(commit, self.adapter.closed)
			self.assertEqual(set(self.adapter.named("close_checks")), {jobs.STOPPED})
			# Not while a review of the commit runs: that review ends its check.
			self.running_review("review")
			self.adapter.closed.clear()
			jobs.close_stuck_checks()
			self.assertNotIn(commit, self.adapter.closed)

	def test_a_claim_about_a_library_is_held_back(self):
		# pilot#687 and cargo#110: P1s about boto3 and frappe.log_error that the code did not prove.
		claim = dict(PROBLEM, severity="P1", title="upload_fileobj raises S3UploadFailedError")
		answers = {"verify": '[{"index": 0, "valid": true, "confidence": 0.9, "evidence": "library"}]'}
		self.online_review(
			[[("report_finding", claim), ("submit_review", {"summary": "s", "confidence_score": 3})]], answers
		)
		row = db.values(
			"Review Finding", {"pull_request": self.pr}, ("severity", "status", "status_reason", "confidence")
		)
		self.assertEqual(
			row,
			{
				"severity": "P2",
				"status": "Dismissed",
				"status_reason": "rests on library behavior that the code does not show",
				"confidence": 0.5,
			},
		)

	def running_review(self, stage: str, attempt: int = 0) -> str:
		from frappe.utils import now_datetime

		return str(
			frappe.get_doc(
				{
					"doctype": "Code Review",
					"pull_request": self.pr,
					"repository": self.repository,
					"source": "pull_request",
					"status": "Running",
					"stage": stage,
					"attempt": attempt,
					"trigger": "open",
					"head_sha": self.head,
					"started_at": now_datetime(),
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def online_review(self, steps, answers, manual=False):
		"""Run a review with the clients online, and return every model request."""
		requests = []
		respond = scripted(steps, answers)

		def record(request):
			requests.append(request)
			return respond(request)

		with (
			patch.object(LLMClient, "online", new_callable=PropertyMock, return_value=True),
			use_responder(record),
		):
			trigger.consider(self.pr, "open", manual=manual, requested_by="ada" if manual else "")
		return requests

	def test_rereview_posts_p2_on_new_lines(self):
		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		models = (self.repo.path / "app/models.py").read_text() + "\n\nVALUE = 1\n"
		self.repo.commit({"app/models.py": models}, "add value")
		frappe.db.set_value("Pull Request", self.pr, "head_sha", self.repo.head())
		nit = {
			"path": "app/models.py",
			"start_line": len(models.splitlines()),
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
		self.assertEqual(self.row(second)["posted_count"], 1)

	def test_project_test_rule_without_a_model_finding(self):
		self.commit_on_main({"CLAUDE.md": "- Always add tests for behavior changes.\n"})
		name = self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		review = self.row(name)
		self.assertEqual(review["posted_count"], 1)
		# A missing test is a P2 rule break: fix it before merging, so 4.
		self.assertEqual(review["confidence_score"], 4)
		body = self.adapter.named("create_review")[0][0].body
		self.assertIn("Code changes without tests", body)
		self.assertIn("**Project rule:** [CLAUDE.md line 1](https://github.test/", body)

	def test_verifier_keeps_p0_for_a_proven_break(self):
		finding = {
			"path": "app/models.py",
			"start_line": 5,
			"severity": "P0",
			"comment_type": "logic",
			"title": "change_email now raises TypeError",
			"body": "x",
			"confidence": 0.9,
		}
		lower = '[{"index": 0, "valid": true, "severity": "P1", "confidence": 0.8}]'
		# The test site has no gateway: put the clients online so the verifier runs.
		with (
			patch.object(LLMClient, "online", new_callable=PropertyMock, return_value=True),
			use_responder(
				scripted(
					[
						[
							("report_finding", finding),
							("submit_review", {"summary": "s", "confidence_score": 2}),
						]
					],
					{"verify": lower},
				)
			),
		):
			trigger.consider(self.pr, "open")
		severities = frappe.get_all("Review Finding", filters={"pull_request": self.pr}, pluck="severity")
		self.assertEqual(severities, ["P0"])

	def test_gate_skips_drafts_and_disabled_repositories(self):
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 1)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 0)
		frappe.db.set_value("Code Repository", self.repository, "reviews_enabled", 0)
		self.assertIsNone(trigger.consider(self.pr, "open"))

	def test_skipped_review_marks_the_commit(self):
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 1)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		self.assertEqual(self.adapter.named("status"), ["skipped"])
		# A repository without reviews gets no check.
		frappe.db.set_value("Code Repository", self.repository, "reviews_enabled", 0)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		self.assertEqual(self.adapter.named("status"), ["skipped"])

	def test_skip_keeps_the_check_of_a_reviewed_commit(self):
		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		statuses = self.adapter.named("status")
		self.assertEqual(statuses, ["queued", "pending", "success"])
		# A skipped check counts as passing on GitHub. It must not replace the result of a review.
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 1)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		self.assertEqual(self.adapter.named("status"), statuses)

	def test_skip_marks_a_new_commit_while_an_older_review_runs(self):
		# The running review ends the check of its own commit, not the check of the new one.
		older = self.running_review("review")
		frappe.db.set_value("Code Review", older, "head_sha", self.base)
		frappe.db.set_value("Pull Request", self.pr, "is_draft", 1)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		self.assertEqual(self.adapter.named("status"), ["skipped"])
		# A queued review fetches the newest head when it starts, so it owns the check.
		frappe.db.set_value("Code Review", older, "status", "Queued")
		self.assertIsNone(trigger.consider(self.pr, "open"))
		self.assertEqual(self.adapter.named("status"), ["skipped"])
		# Not when the queued review is cancelled: it will not review the commit.
		frappe.db.set_value("Code Review", older, "cancel_requested", 1)
		self.assertIsNone(trigger.consider(self.pr, "open"))
		self.assertEqual(self.adapter.named("status"), ["skipped", "skipped"])

	def test_credit_cap_marks_the_check_skipped(self):
		cap = db.single_int("Guard Settings", "monthly_credit_cap", 0)
		frappe.db.set_single_value("Guard Settings", "monthly_credit_cap", 1)
		try:
			name = self.review([])
		finally:
			# Later tests in this class would hit the cap.
			frappe.db.set_single_value("Guard Settings", "monthly_credit_cap", cap)
		self.assertEqual(self.row(name)["status"], "Skipped")
		self.assertEqual(self.adapter.named("status"), ["queued", "pending", "skipped"])

	def test_skip_authors_from_review_settings(self):
		settings = frappe.get_doc("Review Settings", "Organization")
		settings.set("exclude_authors", "mergify[bot]\nad*")
		settings.save(ignore_permissions=True)
		try:
			self.assertIsNone(trigger.consider(self.pr, "open"))
			# Skipped authors apply to requested reviews too.
			self.assertIsNone(trigger.consider(self.pr, "open", manual=True, requested_by="bob"))
		finally:
			settings.set("exclude_authors", "")
			settings.save(ignore_permissions=True)
		with use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])):
			self.assertTrue(trigger.consider(self.pr, "open"))

	def commit_on_main(self, files):
		"""Commit on the base branch and merge it into the pull request."""
		self.repo.git("checkout", "-q", "main")
		self.repo.commit(files, "on main")
		self.repo.git("checkout", "-q", "feature")
		self.repo.git("merge", "-q", "main", "-m", "merge main")
		frappe.db.set_value("Pull Request", self.pr, "head_sha", self.repo.head())

	def test_auto_approve_clean_low_risk(self):
		self.commit_on_main({".guard/config.toml": AUTO_APPROVE})
		name = self.review(
			[[("submit_review", {"summary": "Small change.", "confidence_score": 5, "risk_level": "low"})]]
		)
		self.assertEqual(frappe.db.get_value("Code Review", name, "status"), "Completed")
		self.assertTrue(self.adapter.named("approve"))
		self.assertEqual(self.adapter.named("status")[-1], "success")
		self.assertTrue(db.value("Pull Request", self.pr, "guard_approved"))

	def test_auto_approve_waits_for_earlier_findings(self):
		self.commit_on_main({".guard/config.toml": AUTO_APPROVE})
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
		# A finding shown only in the summary still blocks approval.
		frappe.db.set_value("Review Finding", {"pull_request": self.pr}, "posted", 0)
		clean = {"summary": "s", "confidence_score": 5, "risk_level": "low"}
		with use_responder(scripted([[("submit_review", clean)]])):
			second = trigger.consider(self.pr, "open", manual=True, requested_by="ada")
		# erpnext #62149: the earlier P1 still applies, so the score and the summary count it,
		# as the status check does.
		self.assertEqual(self.row(second)["confidence_score"], 3)
		summary = self.adapter.comments[self.adapter.named("update_comment")[-1]]
		self.assertIn("Confidence **3/5**", summary)
		self.assertIn("no new findings · 1 open from earlier reviews (1 P1)", summary)
		self.assertEqual(self.adapter.named("status")[-1], "failure")
		self.assertFalse(self.adapter.named("approve"))

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

	def test_mention_runs_tool_calls_that_came_back_as_text(self):
		from guard.conversation import threads

		requests: list[ChatRequest] = []
		responder = scripted([dsml([("read_file", {"path": "app/models.py"})]), "It adds `notify`."])

		def respond(request: ChatRequest) -> ChatResult:
			requests.append(request)
			return responder(request)

		comment = ProviderComment("c5", "issue", "ada", "@guard why this signature?", number=7)
		with use_responder(respond), patch("guard.llm.client.LLMClient.online", new=True):
			threads.handle(self.pr, comment)
		self.assertEqual(self.adapter.named("reply"), ["It adds `notify`."])
		tools = [m.content for m in requests[-1].messages if m.role == "tool"]
		self.assertIn("def update_user_email", tools[0])

	def test_mention_never_posts_tool_markup(self):
		from guard.conversation import answer, threads

		# Every turn asks for a tool, and the last call, which offers no tools, does as well.
		leaked = dsml([("read_file", {"path": "app/models.py"})])
		responder = scripted([leaked] * answer.MAX_TURNS, {"conversation": leaked})
		comment = ProviderComment("c6", "issue", "ada", "@guard is this safe?", number=7)
		with use_responder(responder), patch("guard.llm.client.LLMClient.online", new=True):
			threads.handle(self.pr, comment)
		self.assertFalse(self.adapter.named("reply"))
		self.assertEqual(self.on_comment("c6")[-1], "confused")

	def test_chat_runs_tool_calls_that_came_back_as_text(self):
		from guard.conversation import chat

		responder = scripted([dsml([("read_file", {"path": "app/models.py"})]), "It adds `notify`."])
		with use_responder(responder):
			result = chat.send(self.pr, "Why this signature?")
		self.assertEqual(result["answer"], "It adds `notify`.")

	def test_replies_of_the_github_app_are_guard_comments(self):
		from guard.patches import mark_guard_comments
		from guard.webhooks import handler
		from guard.webhooks.events import WebhookEvent

		provider = db.value("Code Repository", self.repository, "code_provider")
		db.set_values("Code Provider", provider, {"app_slug": "guard-test", "enabled": 1})
		self.addCleanup(db.set_values, "Code Provider", provider, {"app_slug": None})

		def reply(comment_id: str) -> ProviderComment:
			# A plain reply has no `<!-- guard:` marker.
			return ProviderComment(comment_id, "inline", "guard-test[bot]", "Checked.", is_bot=True, number=7)

		handler._comment(self.repository, WebhookEvent("comment", "created", number=7, comment=reply("g1")))
		handler.store_comment(self.pr, self.repository, reply("g2"), False)  # stored before the fix
		mark_guard_comments.execute()
		for comment_id in ("g1", "g2"):
			filters = {"pull_request": self.pr, "provider_comment_id": comment_id}
			self.assertEqual(db.value("Pull Request Comment", filters, "is_guard"), 1)

	def on_comment(self, comment_id):
		"""Reactions on one comment, in order: added contents and "removed"."""
		return [r[2] for r in self.adapter.named("reaction") if r[1] == comment_id]

	def test_mention_reactions_for_a_question(self):
		from guard.conversation import threads

		comment = ProviderComment("c2", "issue", "ada", "@guard is this safe?", number=7)
		with use_responder(scripted(["Yes."])), patch("guard.llm.client.LLMClient.online", new=True):
			threads.handle(self.pr, comment)
		self.assertEqual(self.on_comment("c2"), ["eyes", "removed", "+1"])

	def test_mention_reactions_for_a_review(self):
		from guard.conversation import threads

		comment = ProviderComment("c3", "issue", "ada", "@guard review", number=7)
		with use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 4})]])):
			threads.handle(self.pr, comment)
		self.assertEqual(self.on_comment("c3"), ["eyes", "removed", "+1"])

	def test_mention_reactions_when_the_review_is_skipped(self):
		from guard.conversation import threads

		frappe.db.set_value("Code Repository", self.repository, "reviews_enabled", 0)
		threads.handle(self.pr, ProviderComment("c4", "issue", "ada", "@guard review", number=7))
		self.assertEqual(self.on_comment("c4"), ["eyes", "removed"])

	def mention_replaced_by_push(self, comment_id):
		"""A mention asks for a review, and a push replaces that review while the model runs."""
		from guard.conversation import threads
		from guard.review.jobs import create_review

		newer = []

		def push_then_submit(request):
			if not newer:
				with patch("guard.review.jobs.enqueue_review"):
					newer.append(create_review(self.pr, "push"))
			return respond(request)

		respond = scripted([[("submit_review", {"summary": "s", "confidence_score": 4})]])
		with use_responder(push_then_submit):
			threads.handle(self.pr, ProviderComment(comment_id, "issue", "ada", "@guard review", number=7))
		requested = db.value("Code Review", {"pull_request": self.pr, "trigger": "mention"}, "status")
		self.assertEqual(requested, "Cancelled")
		return newer[0]

	def test_mention_waits_for_the_review_that_replaced_its_own(self):
		# suite #881: a push came while the requested review ran. That is no failure of the request.
		push = self.mention_replaced_by_push("c5")
		self.assertEqual(self.on_comment("c5"), ["eyes"])
		with use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 4})]])):
			ReviewPipeline.run(push)
		self.assertEqual(self.row(push)["status"], "Completed")
		self.assertEqual(self.on_comment("c5"), ["eyes", "removed", "+1"])

	def test_review_finishes_every_waiting_mention(self):
		from guard.conversation import threads

		self.mention_replaced_by_push("c6")
		# The same person asks again. Their first mention must not keep 👀.
		with use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 4})]])):
			threads.handle(self.pr, ProviderComment("c7", "issue", "ada", "@guard review", number=7))
		self.assertEqual(self.on_comment("c6"), ["eyes", "removed", "+1"])
		self.assertEqual(self.on_comment("c7"), ["eyes", "removed", "+1"])

	def test_review_goes_on_from_its_checkpoint_after_a_busy_gateway(self):
		from guard.review import checkpoint

		# suite #1081: a 429 after 57 turns sent the review back to the start.
		answer = scripted(
			[
				[("read_file", {"path": "app/models.py"}), ("report_finding", MISSED_NOTIFY)],
				[("submit_review", {"summary": "s", "confidence_score": 3})],
			]
		)
		sent: list[int] = []

		def busy_on_turn_two(request: ChatRequest) -> ChatResult:
			if request.purpose == "review" and request.tools:
				sent.append(len(request.messages))
				if len(sent) == 2:
					raise GatewayBusy("429", 5)
			return answer(request)

		with use_responder(busy_on_turn_two):
			name = trigger.consider(self.pr, "open")
		self.assertEqual(self.row(name)["status"], "Completed")
		# System and task, then the answer and two tool outputs. The next job sends those 5 again.
		self.assertEqual(sent, [2, 5, 5])
		titles = frappe.get_all("Review Finding", filters={"code_review": name}, pluck="title")
		self.assertIn(MISSED_NOTIFY["title"], titles)
		self.assertFalse(checkpoint._path(str(name)).exists(), "an ended review keeps no checkpoint")

	def test_review_that_stops_after_the_agents_does_not_run_them_again(self):
		submit = ("submit_review", {"summary": "s", "confidence_score": 3})
		answer = scripted([[("report_finding", MISSED_NOTIFY), submit]])
		purposes: list[str] = []

		def respond(request: ChatRequest) -> ChatResult:
			purposes.append(request.purpose)
			return answer(request)

		with (
			use_responder(respond),
			patch("guard.review.pipeline.verify.verify", side_effect=[GatewayBusy("429", 5), None]) as check,
		):
			name = trigger.consider(self.pr, "open")
		self.assertEqual(self.row(name)["status"], "Completed")
		self.assertEqual(check.call_count, 2)
		self.assertEqual(purposes.count("review"), 1, "the second job used the saved findings and verdict")
		titles = frappe.get_all("Review Finding", filters={"code_review": name}, pluck="title")
		self.assertIn(MISSED_NOTIFY["title"], titles)

	def test_cancelled_review_does_not_wait_for_a_slot(self):
		waiting = self.running_review("")
		db.set_values("Code Review", waiting, {"status": "Queued", "cancel_requested": 1})
		running = self.running_review("review")
		# Rows outlive the test: fail_dead_reviews in a later test would start this one again.
		self.addCleanup(db.set_values, "Code Review", running, {"status": "Completed"})
		frappe.db.set_single_value("Guard Settings", "max_concurrent_reviews", 1)
		self.addCleanup(frappe.db.set_single_value, "Guard Settings", "max_concurrent_reviews", 4)
		with patch("guard.review.jobs.enqueue_review") as enqueue:
			ReviewPipeline.run(waiting)
		self.assertEqual(self.row(waiting)["status"], "Cancelled")
		enqueue.assert_not_called()

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


class TestApexAreas(IntegrationTestCase):
	def test_a_stopped_apex_review_goes_on_with_the_pass_across_the_parts(self):
		from guard.review import checkpoint

		name = f"test-{frappe.generate_hash(length=8)}"
		self.addCleanup(checkpoint.delete, name)
		diff = "".join(
			f"diff --git a/app/f{n}.py b/app/f{n}.py\n--- a/app/f{n}.py\n+++ b/app/f{n}.py\n"
			+ f"@@ -1 +1 @@\n-a = {n}\n+a = {n + 1}\n"
			for n in range(20)
		)
		sent: list[str] = []

		def respond(request: ChatRequest) -> ChatResult:
			across = "Verdicts of the parts" in request.messages[1].content
			sent.append("across" if across else "part")
			if across and sent.count("across") == 1:
				raise GatewayBusy("429", 5)
			summary = "whole" if across else "part"
			return ChatResult(
				"",
				[ToolCall("t", "submit_review", {"summary": summary, "confidence_score": 5})],
				Usage(10, 1),
			)

		def job() -> tuple[ReviewPipeline, ReviewRun]:
			run = ReviewRun(
				SimpleNamespace(name=name),
				None,
				"",
				lambda path: "a = 1",
				lambda path: None,
				lambda path: [],
				"",
				"",
			)
			run.tier = "apex"
			run.files = parse_diff(diff)
			pipeline = ReviewPipeline.__new__(ReviewPipeline)
			pipeline.check_cancel = lambda lock=False: None
			pipeline.save_usage = lambda: None
			pipeline.current = run
			pipeline.started = time.time()
			return pipeline, run

		with use_responder(respond):
			first, run = job()
			with self.assertRaises(GatewayBusy):
				first.agent(run, BuiltContext(ContextPack()))
			second, run = job()
			second.agent(run, BuiltContext(ContextPack()))
		# 20 files make 3 parts. The second job does not review them again.
		self.assertEqual(sent, ["part", "part", "part", "across", "across"])
		self.assertEqual(run.verdict.summary, "whole")
		self.assertEqual(len(run.files), 20)

	def test_each_part_gets_its_own_turns_and_the_riskiest_files_come_first(self):
		files = 60
		diff = "".join(
			f"diff --git a/app/f{n}.py b/app/f{n}.py\n--- a/app/f{n}.py\n+++ b/app/f{n}.py\n"
			+ f"@@ -1 +1 @@\n-a = {n}\n+a = {n + 1}\n"
			for n in range(files)
		)
		run = ReviewRun(None, None, "", lambda path: "a = 1", lambda path: None, lambda path: [], "", "")
		run.tier = "apex"
		run.files = parse_diff(diff)
		caller = Link(None, "f", "f", "app/x.py", 3, True)
		run.changed = [ChangedEntity("app/f59.py", "function", "f", "m.f", "modified", broken=[caller])]
		tasks: list[str] = []

		def respond(request: ChatRequest) -> ChatResult:
			task = request.messages[1].content
			tasks.append(task)
			summary = "whole" if "Verdicts of the parts" in task else f"part {len(tasks)}"
			return ChatResult(
				"",
				[ToolCall("t", "submit_review", {"summary": summary, "confidence_score": 5})],
				Usage(10, 1),
			)

		pipeline = ReviewPipeline.__new__(ReviewPipeline)
		pipeline.check_cancel = lambda lock=False: None
		pipeline.current = None
		pipeline.started = time.time()
		with use_responder(respond):
			pipeline.agent(run, BuiltContext(ContextPack()))
		parts = [t for t in tasks if "diff part" in t]
		[across] = [t for t in tasks if "Verdicts of the parts" in t]
		self.assertEqual(len(parts), pipeline_module.MAX_AREAS, "every part runs, not only the first")
		self.assertIn("### app/f59.py", next(t for t in parts if "diff part (1)" in t))
		# 6 parts of 8 files leave 12 files to the pass across the parts.
		self.assertIn("No part reviewed these files", across)
		self.assertEqual(
			across.count("\n- app/f"), files - pipeline_module.MAX_AREAS * pipeline_module.AREA_FILES
		)
		self.assertIn("(score 5): part", across)
		self.assertEqual(run.verdict.summary, "whole")
		self.assertEqual(len(run.files), files)

	def test_parts_get_the_context_budget_of_plus(self):
		# Each part sends its task again on every turn, and reviews at most 8 files.
		diff = "".join(
			f"diff --git a/app/f{n}.py b/app/f{n}.py\n--- a/app/f{n}.py\n+++ b/app/f{n}.py\n"
			+ f"@@ -1 +1 @@\n-a = {n}\n+a = {n + 1}\n"
			for n in range(pipeline_module.AREA_FILES + 1)
		)
		run = ReviewRun(None, None, "", lambda path: "a = 1", lambda path: None, lambda path: [], "", "")
		run.tier = "apex"
		run.files = parse_diff(diff)
		# Fits the apex budget, but not the one of plus.
		big = ContextFile("app/big.py", "x = 1\n" * 16_000, "impacted by f")
		tasks: list[str] = []

		def respond(request: ChatRequest) -> ChatResult:
			tasks.append(request.messages[1].content)
			return ChatResult(
				"", [ToolCall("t", "submit_review", {"summary": "s", "confidence_score": 5})], Usage(10, 1)
			)

		pipeline = ReviewPipeline.__new__(ReviewPipeline)
		pipeline.check_cancel = lambda lock=False: None
		pipeline.current = None
		pipeline.started = time.time()
		with use_responder(respond):
			pipeline.agent(run, BuiltContext(ContextPack(files=[big])))
		parts = [t for t in tasks if "diff part" in t]
		[across] = [t for t in tasks if "Verdicts of the parts" in t]
		self.assertEqual(len(parts), 2)
		self.assertFalse(any("app/big.py" in t for t in parts))
		self.assertIn("app/big.py", across)

	def test_apex_findings_and_score_are_checked_by_the_plus_model(self):
		from guard.review import verify

		run = ReviewRun(None, None, "", lambda path: "a = 1", lambda path: None, lambda path: [], "", "")
		run.tier = "apex"
		run.findings = [Finding("app/a.py", 1, 1, "P1", "logic", "Wrong value", "x")]
		run.verdict = Verdict(summary="s", confidence_score=3, submitted=True)
		models = {"base": "base-model", "plus": "plus-model", "apex": "apex-model"}
		gateway = GatewayConfig("https://gw", "k", "anthropic", 30, models)
		answers = {"verify": '[{"index": 0, "valid": false, "reason": "no"}]', "verdict": '{"score": 5}'}
		respond = scripted([], answers)
		requests: list[ChatRequest] = []

		def record(request: ChatRequest) -> ChatResult:
			requests.append(request)
			return respond(request)

		with patch("guard.settings.gateway", return_value=gateway), use_responder(record):
			verify.verify(run)
			verify.check_verdict(run)
		self.assertEqual(
			[(r.purpose, r.model) for r in requests], [("verify", "plus-model"), ("verdict", "plus-model")]
		)
		self.assertEqual(run.verdict.confidence_score, 5)
		# Without a plus model, the check falls back to apex and not to base.
		gateway.models = dict(models, plus="")
		with patch("guard.settings.gateway", return_value=gateway):
			self.assertEqual(verify.check_tier("apex"), "apex")
			self.assertEqual(verify.check_tier("plus"), "plus")


class TestDeadline(UnitTestCase):
	def test_a_new_review_can_become_apex_so_it_gets_the_long_timeout(self):
		self.assertEqual(jobs.job_timeout(""), jobs.job_timeout("apex"))
		self.assertLess(jobs.job_timeout("plus"), jobs.job_timeout(""))

	def test_the_job_timeout_bounds_the_deadline(self):
		pipeline = ReviewPipeline.__new__(ReviewPipeline)
		pipeline.started = 1000.0
		job = SimpleNamespace(timeout=1800)
		with patch("rq.get_current_job", return_value=job):
			self.assertEqual(pipeline.deadline("apex"), 1000 + 1800 - pipeline_module.FINISH_SECONDS)
		with patch("rq.get_current_job", return_value=None):
			self.assertEqual(pipeline.deadline("apex"), 1000 + 7200 - pipeline_module.FINISH_SECONDS)

	def test_no_turn_starts_after_the_deadline(self):
		run = ReviewRun(None, None, "", lambda path: "a = 1", lambda path: None, lambda path: [], "", "")
		calls: list[ChatRequest] = []

		def respond(request: ChatRequest) -> ChatResult:
			calls.append(request)
			return ChatResult("", [ToolCall("t", "read_file", {"path": "a.py"})], Usage(10, 1))

		with use_responder(respond):
			result = Agent(run, "s", 8).run_loop("task", deadline=time.time() - 1)
		self.assertEqual((result.turns, len(calls)), (0, 0))


class TestWebhookDeadlock(IntegrationTestCase):
	def test_a_deadlock_goes_to_the_job_retry(self):
		"""Frappe runs a job again after a deadlock. The handler must not mark the delivery Failed first."""
		from guard.webhooks import handler

		delivery = frappe.get_doc({"doctype": "Webhook Delivery", "event": "ping", "payload": "{}"}).insert(
			ignore_permissions=True
		)
		with (
			patch("guard.webhooks.github.parse", side_effect=frappe.QueryDeadlockError("deadlock")),
			self.assertRaises(frappe.QueryDeadlockError),
		):
			handler.process(str(delivery.name))
		self.assertEqual(db.value("Webhook Delivery", str(delivery.name), "status"), "Received")

	def test_a_delivery_that_lost_its_job_is_handled_again(self):
		# guard 26 Oct: a deploy lost the jobs of about 300 deliveries, and they stayed Received.
		from frappe.utils import add_to_date, now_datetime

		from guard.webhooks import handler

		def delivery(minutes_ago: int, attempt: int = 0) -> str:
			name = str(
				frappe.get_doc(
					{"doctype": "Webhook Delivery", "event": "ping", "payload": "{}", "attempt": attempt}
				)
				.insert(ignore_permissions=True)
				.name
			)
			created = add_to_date(now_datetime(), minutes=-minutes_ago)
			frappe.db.set_value("Webhook Delivery", name, "creation", created, update_modified=False)
			return name

		lost, queued, fresh, old, stuck = (
			delivery(10),
			delivery(10),
			delivery(1),
			delivery(7 * 60),
			delivery(10, 3),
		)
		with (
			patch.object(
				handler, "is_job_enqueued", side_effect=lambda job: job == handler.delivery_job_id(queued)
			),
			patch.object(handler, "enqueue_delivery") as enqueue,
		):
			handler.requeue_lost_deliveries()
		self.assertEqual(
			[c.args[0] for c in enqueue.call_args_list if c.args[0] in (lost, queued, fresh, old, stuck)],
			[lost],
		)
		self.assertEqual(db.value("Webhook Delivery", lost, "attempt"), 1)
		self.assertEqual(db.value("Webhook Delivery", stuck, "status"), "Failed")
		self.assertEqual(db.value("Webhook Delivery", old, "status"), "Received")


class TestCheckpoint(UnitTestCase):
	def agent(self) -> Agent:
		run = ReviewRun(None, None, "", lambda path: "a = 1", lambda path: None, lambda path: [], "h", "b")
		return Agent(run, "system", 8)

	def test_an_agent_comes_back_as_it_was(self):
		from guard.review import checkpoint

		agent = self.agent()
		calls = [ToolCall("t1", "read_file", {"path": "a.py"}), ToolCall("t2", "report_finding", PROBLEM)]
		agent.messages.append(Message("user", "task", cache=True))
		agent.request()
		thinking = [{"type": "thinking", "thinking": "", "signature": "s1"}]
		agent.receive(ChatResult("thinking", calls, Usage(10, 1, 4), thinking=thinking))
		again = self.agent()
		checkpoint.restore_agent(again, checkpoint.dump_agent(agent))
		self.assertEqual(again.messages, agent.messages)
		self.assertEqual(
			(again.turns, again.reported, again.tool_turns, again.caching),
			(1, agent.reported, [[3, 4]], True),
		)
		# The cache mark is on the same message, and only there: the next turn moves it.
		self.assertIs(again.breakpoint, again.messages[-1])
		again.receive(ChatResult("", [ToolCall("t3", "list_directory", {"path": ""})], Usage(1, 1)))
		self.assertEqual([m.cache for m in again.messages].count(True), 3)  # system, task, newest

	def test_a_checkpoint_of_another_head_or_prompt_is_not_used(self):
		from guard.review import checkpoint

		agent = self.agent()
		key = checkpoint.key(agent.run, "system", [], [])
		self.assertNotEqual(key, checkpoint.key(agent.run, "another system", [], []))
		agent.run.head_sha = "newer"
		self.assertNotEqual(key, checkpoint.key(agent.run, "system", [], []))
		name = f"test-{frappe.generate_hash(length=8)}"
		self.addCleanup(checkpoint.delete, name)
		checkpoint.save(name, {"key": key, "phase": "final"})
		self.assertEqual(checkpoint.load(name, key), {"key": key, "phase": "final"})
		self.assertIsNone(checkpoint.load(name, "other"))
		self.assertIsNone(checkpoint.load("no-such-review", key))
