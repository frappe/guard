"""User actions show their result at once: in the database, on the provider and in Desk."""

from typing import Any
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from guard.api import ui
from guard.conversation import threads
from guard.indexing import indexer
from guard.indexing.jobs import index_for
from guard.knowledge import builder
from guard.llm.client import use_responder
from guard.llm.types import GatewayBusy
from guard.providers.base import ProviderComment
from guard.review import jobs, state
from guard.review.jobs import create_review
from guard.review.pipeline import ReviewPipeline
from guard.tests.cases import PullRequestCase
from guard.tests.fakes import scripted
from guard.utils import db

SUBMIT: list[list[tuple[str, dict[str, Any]]] | str] = [
	[("submit_review", {"summary": "s", "confidence_score": 5})]
]
FINDING = {
	"path": "app/models.py",
	"start_line": 5,
	"severity": "P1",
	"comment_type": "logic",
	"title": "Caller change_email is not updated for the new notify argument",
	"body": "`app/service.py:6` calls `update_user_email(user, email)`. It raises TypeError now.",
	"confidence": 0.9,
}


class TestCancel(PullRequestCase):
	def queued_review(self) -> str:
		"""A review that waits in the queue: no worker has started it."""
		with patch("guard.review.jobs.enqueue_review"):
			return create_review(self.pr, "manual")

	def test_a_new_review_shows_a_queued_check_at_once(self):
		name = self.queued_review()
		check = db.value("Code Review", name, "check_run_id")
		self.assertTrue(check)
		self.assertEqual(self.adapter.checks, [(self.head, "queued", state.QUEUED_TEXT, "")])

	def test_cancel_shows_at_once_and_the_late_job_does_not_undo_it(self):
		name = self.queued_review()
		check = db.value("Code Review", name, "check_run_id")
		self.assertEqual(ui.cancel_review(name), "Cancelled")
		row = db.values("Code Review", name, ("status", "cancelled_by", "completed_at")) or {}
		self.assertEqual((row["status"], row["cancelled_by"]), ("Cancelled", "Administrator"))
		self.assertTrue(row["completed_at"])
		self.assertEqual(
			self.adapter.checks[-1],
			(self.head, "cancelled", "Guard stopped this review: Administrator cancelled it.", check),
		)
		# The job of the review runs later, when a worker is free. It stops, and changes nothing.
		self.adapter.calls.clear()
		with use_responder(scripted(SUBMIT)):
			ReviewPipeline.run(name)
		self.assertEqual(db.value("Code Review", name, "status"), "Cancelled")
		self.assertNotIn("pending", self.adapter.named("status"))
		self.assertEqual(self.adapter.named("react"), [])

	def test_a_job_that_a_cancel_interrupts_does_not_queue_the_review_again(self):
		# The user cancels while the model runs, and then the gateway refuses the call.
		def cancel_then_busy(request):
			ui.cancel_review(name)
			raise GatewayBusy("429", 5)

		name = self.queued_review()
		with (
			use_responder(cancel_then_busy),
			patch("guard.review.jobs.enqueue_review") as enqueue,
			patch("guard.review.pipeline.verify.verify"),
		):
			ReviewPipeline.run(name)
		self.assertEqual(db.value("Code Review", name, "status"), "Cancelled")
		enqueue.assert_not_called()

	def test_a_stopped_review_gives_back_the_result_of_the_completed_one(self):
		completed = self.review(SUBMIT)
		check = db.value("Code Review", str(completed), "check_run_id")
		rereview = self.queued_review()
		self.assertEqual(db.value("Code Review", rereview, "check_run_id"), check)
		ui.cancel_review(rereview)
		# Not "stopped": the commit keeps the 5/5 of the review that completed.
		self.assertEqual(self.adapter.checks[-1][1:], ("success", "5/5 · 0 open finding(s)", check))

	def test_a_late_job_of_a_replaced_review_leaves_the_check_of_the_newer_one(self):
		older = self.queued_review()
		with use_responder(scripted(SUBMIT)):
			newer = create_review(self.pr, "manual")
		self.assertEqual(db.value("Code Review", newer, "status"), "Completed")
		result = self.adapter.checks[-1]
		# The job of the older review runs only now, for example from the long queue.
		with use_responder(scripted(SUBMIT)):
			ReviewPipeline.run(older)
		self.assertEqual(db.value("Code Review", older, "status"), "Cancelled")
		self.assertEqual(self.adapter.checks[-1], result)

	def test_a_queued_review_that_lost_its_job_is_queued_again(self):
		name = self.queued_review()
		frappe.db.set_value(
			"Code Review", name, "modified", add_to_date(now_datetime(), minutes=-10), update_modified=False
		)
		with (
			patch("guard.review.jobs.is_job_enqueued", return_value=False),
			patch("guard.review.jobs.enqueue_review") as enqueue,
		):
			jobs.requeue_lost()
		enqueue.assert_any_call(name, attempt=0, tier="")

	def test_cancel_answers_the_mention_that_asked_for_the_review(self):
		with patch("guard.review.jobs.enqueue_review"):
			threads.handle(self.pr, ProviderComment("m1", "issue", "ada", "@guard review", number=7))
		name = frappe.get_last_doc("Code Review", filters={"pull_request": self.pr}).name
		ui.cancel_review(str(name))
		reactions = [r[2] for r in self.adapter.named("reaction") if r[1] == "m1"]
		self.assertEqual(reactions, ["eyes", "removed", "confused"])

	def test_a_review_that_posts_cannot_be_cancelled(self):
		name = self.queued_review()
		db.set_values("Code Review", name, {"status": "Running", "stage": "publish"})
		with self.assertRaises(frappe.ValidationError):
			ui.cancel_review(name)
		self.assertEqual(db.value("Code Review", name, "status"), "Running")
		db.set_values("Code Review", name, {"status": "Completed"})
		with self.assertRaises(frappe.ValidationError):
			ui.cancel_review(name)

	def test_a_newer_review_of_the_commit_takes_over_its_check(self):
		older = self.queued_review()
		check = db.value("Code Review", older, "check_run_id")
		newer = self.queued_review()
		self.assertEqual(db.value("Code Review", older, "status"), "Cancelled")
		self.assertEqual(db.value("Code Review", newer, "check_run_id"), check)
		# The shared check is not ended: it is queued again for the newer review.
		self.assertEqual([c[1] for c in self.adapter.checks], ["queued", "queued"])
		self.assertEqual(self.adapter.checks[-1][3], check)


class TestFindingStatus(PullRequestCase):
	def test_dismiss_resolves_the_thread_and_counts_on_the_check(self):
		name = str(
			self.review(
				[[("report_finding", FINDING), ("submit_review", {"summary": "s", "confidence_score": 5})]]
			)
		)
		finding = frappe.get_all(
			"Review Finding", filters={"code_review": name}, fields=["name", "provider_thread_id"]
		)[0]
		check = db.value("Code Review", name, "check_run_id")
		ui.set_finding_status(finding.name, "Dismissed")
		self.assertEqual(self.adapter.named("resolve"), [finding.provider_thread_id])
		# The score is the cap of the open findings now: none is open, so the check passes.
		self.assertEqual(self.adapter.checks[-1][1:], ("success", "5/5 · 0 open finding(s)", check))
		ui.set_finding_status(finding.name, "Open")
		self.assertEqual(self.adapter.named("unresolve"), [finding.provider_thread_id])
		self.assertEqual(self.adapter.checks[-1][1:], ("failure", "3/5 · 1 open finding(s)", check))

	def reviewed_finding(self) -> tuple[str, str]:
		name = str(self.review([[("report_finding", FINDING), ("submit_review", {"summary": "s"})]]))
		return name, str(frappe.get_all("Review Finding", filters={"code_review": name}, pluck="name")[0])

	def test_dismiss_on_a_token_install_sets_a_new_commit_status(self):
		review, finding = self.reviewed_finding()
		# A commit status has no id, so the review of a token install keeps none.
		db.set_values("Code Review", review, {"check_run_id": ""})
		ui.set_finding_status(finding, "Dismissed")
		self.assertEqual(self.adapter.checks[-1][1:], ("success", "5/5 · 0 open finding(s)", ""))
		# With the check off, the review had no status to set again.
		db.set_values("Code Review", review, {"config_snapshot": frappe.as_json({"status_check": False})})
		count = len(self.adapter.checks)
		ui.set_finding_status(finding, "Open")
		self.assertEqual(len(self.adapter.checks), count)

	def test_dismiss_leaves_a_check_that_the_review_ended_because_the_check_is_off(self):
		review, finding = self.reviewed_finding()
		# The review was queued with the check on, then read a config that turns it off. It ended the
		# check as skipped and kept its id.
		db.set_values("Code Review", review, {"config_snapshot": frappe.as_json({"status_check": False})})
		count = len(self.adapter.checks)
		ui.set_finding_status(finding, "Dismissed")
		self.assertEqual(len(self.adapter.checks), count)

	def test_dismiss_after_a_skipped_review_of_the_head_counts_on_the_check_of_the_commit(self):
		review, finding = self.reviewed_finding()
		head = db.value("Code Review", review, "head_sha")
		later = {"doctype": "Code Review", "pull_request": self.pr, "repository": self.repository}
		frappe.get_doc({**later, "status": "Skipped", "head_sha": head}).insert(ignore_permissions=True)
		ui.set_finding_status(finding, "Dismissed")
		check = db.value("Code Review", review, "check_run_id")
		self.assertEqual(self.adapter.checks[-1][1:], ("success", "5/5 · 0 open finding(s)", check))
		# A newer review with its own check: the commit shows that one.
		skipped = {**later, "status": "Skipped", "head_sha": head, "check_run_id": "check-newer"}
		frappe.get_doc(skipped).insert(ignore_permissions=True)
		ui.set_finding_status(finding, "Open")
		self.assertEqual(self.adapter.checks[-1][1:], ("failure", "3/5 · 1 open finding(s)", "check-newer"))
		# A review that runs owns the check, and sets it when it ends.
		frappe.get_doc({**later, "status": "Running", "head_sha": head}).insert(ignore_permissions=True)
		count = len(self.adapter.checks)
		ui.set_finding_status(finding, "Open")
		self.assertEqual(len(self.adapter.checks), count)


class TestRepositoryStatus(PullRequestCase):
	def test_a_failed_index_is_skipped_for_an_hour_from_the_database(self):
		index = index_for(self.repository)
		db.set_values(
			"Repository Index",
			index,
			{"status": "failed", "error": "parser broke", "completed_at": now_datetime()},
		)
		self.assertEqual(indexer.recent_failure(index), "parser broke")
		db.set_values("Repository Index", index, {"completed_at": add_to_date(now_datetime(), hours=-2)})
		self.assertEqual(indexer.recent_failure(index), "")

	def test_a_knowledge_base_build_shows_queued_until_its_job_runs(self):
		db.set_values("Code Repository", self.repository, {"kb_status": "Ready", "kb_version": "v1"})
		with patch("guard.knowledge.builder.frappe.enqueue"):
			ui.build_knowledge_base(self.repository)
		self.assertEqual(db.value("Code Repository", self.repository, "kb_status"), "Queued")
		# A deploy lost the job. The documents of the last build are still there.
		with patch("guard.knowledge.builder.is_job_enqueued", return_value=False):
			builder.recover_stuck()
		self.assertEqual(db.value("Code Repository", self.repository, "kb_status"), "Ready")
