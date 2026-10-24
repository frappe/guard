from unittest.mock import PropertyMock, patch

import frappe

from guard.llm.client import LLMClient, use_responder
from guard.providers.base import ProviderComment
from guard.review import trigger
from guard.tests.cases import AUTO_APPROVE, PullRequestCase
from guard.tests.fakes import scripted
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
		# No model checks the score here, and a held-back P2 is no proof that it is wrong.
		self.assertEqual(self.row(second)["confidence_score"], 4)

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

	def test_fact_check_keeps_a_score_that_held_back_findings_back(self):
		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		checked = '{"score": 4, "reason": "A naming point is left."}'
		requests = self.online_review(
			[[("report_finding", PROBLEM), ("submit_review", {"summary": "s", "confidence_score": 4})]],
			{"verify": '[{"index": 0, "valid": true}]', "verdict": checked},
			manual=True,
		)
		latest = frappe.get_all(
			"Code Review",
			filters={"pull_request": self.pr},
			fields=["confidence_score"],
			order_by="creation desc",
		)[0]
		self.assertEqual(latest.confidence_score, 4)
		[check] = [r for r in requests if r.purpose == "verdict"]
		self.assertIn("held back: re-reviews post P2 only on new lines", check.messages[-1].content)

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
			frame = frappe.get_all("Code Review", filters={"pull_request": self.pr}, pluck="name")
			frappe.db.set_value("Code Review", frame[0], "cancel_requested", 1)
			return respond(request)

		respond = scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		with use_responder(cancel_then_submit):
			name = trigger.consider(self.pr, "open")
		self.assertEqual(self.row(name)["status"], "Cancelled")
		self.assertEqual(
			self.adapter.named("close_checks"), ["Guard stopped this review: a newer review replaced it."]
		)

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
				"status_reason": "low-confidence P2 at strictness 2",
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
		self.assertEqual(review["confidence_score"], 3)
		self.assertIn("Code changes without tests", self.adapter.named("create_review")[0][0].body)

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
