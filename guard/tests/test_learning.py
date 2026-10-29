from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from guard.api import ui
from guard.conversation import threads
from guard.indexing.vectors import VectorRow, VectorStore
from guard.learning import addressed, feedback, memories, memory_writer, suggestions
from guard.llm.client import ChatRequest, use_responder
from guard.llm.embeddings import Embedder
from guard.llm.types import GatewayBusy
from guard.providers.base import ProviderComment, ProviderError, Reaction, ReviewThread
from guard.review import jobs, permissions, trigger
from guard.tests.cases import PullRequestCase
from guard.tests.fakes import scripted
from guard.tests.utils import TempRepo, make_repository
from guard.utils import cache, db
from guard.webhooks import github, handler, polling

FINDING = {
	"path": "app/models.py",
	"start_line": 5,
	"severity": "P1",
	"comment_type": "logic",
	"title": "update_user_email mutates the cached user",
	"body": "`user` comes from `get_user`. Writing to it changes the shared copy.",
	"confidence": 0.9,
}
CHECK = "Check that get_user results are not None before use."
MEMORY = "In app/**, user dicts are per-request copies. Mutating them is intended."


class LearningCase(PullRequestCase):
	def setUp(self):
		super().setUp()
		frappe.db.set_single_value("Guard Settings", "teach_permission", "Everyone")
		self.requests: list[ChatRequest] = []

	def respond(self, answers, steps=None):
		"""Answer by purpose, and keep every request."""
		responder = scripted(steps or [], answers)

		def capture(request):
			self.requests.append(request)
			return responder(request)

		return use_responder(capture)

	def online(self):
		return patch("guard.llm.client.LLMClient.online", new=True)

	def posted_finding(self) -> tuple[str, str]:
		self.review(
			[[("report_finding", FINDING), ("submit_review", {"summary": "s", "confidence_score": 3})]]
		)
		row = db.values("Review Finding", {"pull_request": self.pr}, ("name", "provider_comment_id")) or {}
		self.assertTrue(row.get("provider_comment_id"))
		return str(row["name"]), str(row["provider_comment_id"])


class TestLearning(LearningCase):
	def memory(self, body):
		return db.values(
			"Custom Context",
			{"body": body, "source": "Learned"},
			("name", "path_scope", "repository_scope", "evidence"),
		)

	def make_memory(self) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Custom Context",
				"context_type": "Pattern",
				"title": "Mutating users",
				"body": MEMORY,
				"status": "Active",
				"source": "Learned",
				"repository_scope": db.value("Code Repository", self.repository, "full_name"),
				"path_scope": "app/**",
			}
		).insert(ignore_permissions=True)
		return str(doc.name)

	def context(self, name):
		return db.values("Custom Context", name, ("status", "status_reason", "active_since")) or {}

	def cite(self, memory, count):
		"""Run a review with `count` findings that cite the memory."""
		findings = [
			{**FINDING, "start_line": line, "title": f"Mutation {line}", "rule_id": f"memory:{memory}"}
			for line in range(1, count + 1)
		]
		self.review(
			[
				[
					*(("report_finding", f) for f in findings),
					("submit_review", {"summary": "s", "confidence_score": 3}),
				]
			]
		)
		return frappe.get_all("Review Finding", filters={"custom_context": memory}, pluck="name")

	def test_memory_reaches_the_reviewer(self):
		memory = self.make_memory()
		with self.respond({}, [[("submit_review", {"summary": "s", "confidence_score": 5})]]):
			trigger.consider(self.pr, "open")
		review = [r for r in self.requests if r.purpose == "review"]
		self.assertIn(
			f"Team memory (rule_id: memory:{memory}): Mutating users: {MEMORY}",
			str(review[0].messages),
		)

	def test_finding_that_cites_a_memory_links_to_it(self):
		memory = self.make_memory()
		self.assertEqual(len(self.cite(memory, 1)), 1)

	def test_rule_metrics_count_addressed_findings_on_merged_pull_requests(self):
		memory = self.make_memory()
		findings = self.cite(memory, 2)
		fields = ("usage_count", "merged_count", "addressed_count", "addressed_rate")
		suggestions.update_rule_metrics()
		self.assertEqual(
			db.values("Custom Context", memory, fields),
			{"usage_count": 2, "merged_count": 0, "addressed_count": 0, "addressed_rate": 0},
		)
		frappe.db.set_value("Pull Request", self.pr, "state", "merged")
		frappe.db.set_value("Review Finding", findings[0], "status", "Addressed")
		suggestions.update_rule_metrics()
		self.assertEqual(
			db.values("Custom Context", memory, fields),
			{"usage_count": 2, "merged_count": 2, "addressed_count": 1, "addressed_rate": 50},
		)

	def test_guard_retires_a_memory_the_team_rejects(self):
		memory = self.make_memory()
		findings = self.cite(memory, 3)
		for name in findings[:2]:
			frappe.db.set_value("Review Finding", name, "downvotes", 1)
		frappe.db.set_value("Review Finding", findings[2], "status", "Addressed")
		self.assertEqual(memories.retire_unhelpful(), [])
		frappe.db.set_value("Review Finding", findings[2], "status", "Ignored")
		self.assertEqual(memories.retire_unhelpful(), [memory])
		retired = self.context(memory)
		self.assertEqual(retired["status"], "Inactive")
		self.assertIn("rejected 3 of the 3 findings", retired["status_reason"])
		# Restoring starts a fresh window: the old rejections no longer count.
		ui.set_context_status(memory, "Active")
		restored = self.context(memory)
		self.assertEqual((restored["status"], restored["status_reason"]), ("Active", ""))
		self.assertTrue(restored["active_since"])
		self.assertEqual(memories.retire_unhelpful(), [])

	def test_retiring_by_hand_records_who(self):
		memory = self.make_memory()
		ui.set_context_status(memory, "Inactive")
		self.assertEqual(
			db.value("Custom Context", memory, "status_reason"),
			f"Set to Inactive by {frappe.session.user}",
		)

	def test_evidence_shows_who_said_what_where(self):
		comment = ProviderComment(
			"h5", "inline", "bob", "This crashes on None.", path="app/models.py", line=5
		)
		missed = feedback.record_missed(self.pr, comment)
		name = memory_writer.save(CHECK, [], self.repository, missed)
		self.assertEqual(memory_writer.save(CHECK, [], self.repository, "deleted-feedback"), name)
		found, gone = ui.get_context_evidence(name)
		self.assertEqual(
			(found["author"], found["kind"], found["body"], found["pull_request_number"]),
			("bob", "Missed", "This crashes on None.", 7),
		)
		self.assertEqual(found["pull_request_url"], "https://github.test/pr/7")
		self.assertTrue(gone["missing"])

	def test_reply_with_context_counts_as_downvote_and_writes_memory(self):
		finding, comment_id = self.posted_finding()
		reply = ProviderComment(
			"r1",
			"inline",
			"bob",
			"Users are per-request copies, so mutating them is fine.",
			path="app/models.py",
			line=5,
			in_reply_to=comment_id,
			number=7,
		)
		memory = "In app/**, user dicts are per-request copies. Do not flag mutating them."
		answers = {
			"reply": '{"class": "human", "sentiment": "Context"}',
			"memory": f'{{"memory": "{memory}", "path_scope": ["app/**"], "useful": true}}',
		}
		with self.respond(answers), self.online():
			threads.handle(self.pr, reply)
		reading = next(r for r in self.requests if r.purpose == "reply")
		self.assertIn(FINDING["title"], str(reading.messages))
		self.assertEqual(db.value("Review Finding", finding, "downvotes"), 1)
		self.assertEqual(db.value("Review Finding", finding, "status"), "Open")
		saved = self.memory(memory) or {}
		self.assertEqual(saved.get("path_scope"), "app/**")
		# A reply to another person gets no answer.
		self.assertFalse(self.adapter.named("reply"))

	def test_ack_reply_marks_addressed_resolves_the_thread_and_reacts(self):
		finding, comment_id = self.posted_finding()
		reply = ProviderComment("r2", "inline", "ada", "Good catch, fixed.", in_reply_to=comment_id, number=7)
		with self.respond({"reply": '{"class": "ack", "sentiment": "Agree"}'}), self.online():
			threads.handle(self.pr, reply)
		self.assertEqual(db.value("Review Finding", finding, "status"), "Addressed")
		# The next review checks only open findings, so the reply must close the thread itself.
		thread = db.value("Review Finding", finding, "provider_thread_id")
		self.assertEqual(self.adapter.named("resolve"), [thread])
		self.assertIn("0 open finding(s)", self.adapter.checks[-1][2])
		self.assertIn(("inline", "r2", "+1"), [r[:3] for r in self.adapter.named("reaction")])
		self.assertEqual(len([r for r in self.requests if r.purpose == "reply"]), 1)

	def test_promise_to_fix_keeps_the_finding_open(self):
		finding, comment_id = self.posted_finding()
		reply = ProviderComment("r3", "inline", "ada", "Will do.", in_reply_to=comment_id, number=7)
		with self.respond({"reply": '{"class": "ack", "sentiment": "Neutral"}'}), self.online():
			threads.handle(self.pr, reply)
		reading = next(r for r in self.requests if r.purpose == "reply")
		self.assertIn('a promise to fix it later ("will do")', str(reading.messages))
		self.assertEqual(db.value("Review Finding", finding, "status"), "Open")
		self.assertFalse(self.adapter.named("resolve"))
		self.assertIn(("inline", "r3", "+1"), [r[:3] for r in self.adapter.named("reaction")])

	def test_reactions_count_one_vote_for_each_person(self):
		finding, comment_id = self.posted_finding()
		frappe.db.set_single_value("Guard Settings", "bot_logins", "guard-bot")
		self.adapter.reactions[comment_id] = [
			Reaction("+1", "bob"),
			Reaction("heart", "bob"),
			Reaction("confused", "carol"),
			Reaction("laugh", "dan"),
			Reaction("-1", "guard-bot"),
		]
		addressed.sync_reactions(finding, comment_id, self.repository, self.adapter)
		votes = db.values("Review Finding", finding, ("upvotes", "downvotes")) or {}
		self.assertEqual((votes.get("upvotes"), votes.get("downvotes")), (1, 1))
		self.adapter.reactions[comment_id] = [Reaction("+1", "bob")]
		addressed.sync_reactions(finding, comment_id, self.repository, self.adapter)
		self.assertEqual(db.value("Review Finding", finding, "downvotes"), 0)

	def test_reactions_need_teach_permission(self):
		finding, comment_id = self.posted_finding()
		frappe.db.set_single_value("Guard Settings", "teach_permission", "Guard users")
		self.adapter.reactions[comment_id] = [Reaction("-1", "stranger")]
		addressed.sync_reactions(finding, comment_id, self.repository, self.adapter)
		self.assertEqual(db.value("Review Finding", finding, "downvotes"), 0)

	def test_memory_merges_only_with_memories_of_the_same_repository(self):
		other_repo = TempRepo({"README.md": "other"})
		self.addCleanup(other_repo.cleanup)
		other = make_repository(other_repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		first = memory_writer.save(CHECK, [], self.repository, "feedback-1")
		self.assertEqual(memory_writer.save(CHECK, [], self.repository, "feedback-2"), first)
		self.assertNotEqual(memory_writer.save(CHECK, [], other, "feedback-3"), first)
		evidence = frappe.parse_json(db.value("Custom Context", first, "evidence"))
		self.assertEqual(evidence, ["feedback-1", "feedback-2"])

	def test_memory_does_not_merge_into_a_rule(self):
		rule = "Never call the payment gateway inside a database transaction."
		doc = frappe.get_doc(
			{
				"doctype": "Custom Context",
				"context_type": "Rule",
				"title": rule,
				"body": rule,
				"status": "Suggested",
				"source": "Suggested",
			}
		).insert(ignore_permissions=True)
		vector = Embedder().embed_one(rule)
		VectorStore().upsert([VectorRow(str(doc.name), "context", str(doc.name), self.repository, vector)])
		self.assertNotEqual(memory_writer.save(rule, [], self.repository, "feedback-1"), doc.name)

	def test_human_comment_on_a_missed_issue_becomes_memory(self):
		self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
		comment = ProviderComment(
			"h1",
			"inline",
			"bob",
			"get_user can return None for a deleted user, and this crashes.",
			path="app/models.py",
			line=5,
			number=7,
		)
		answers = {"missed": f'{{"missed": true, "memory": "{CHECK}", "path_scope": ["app/**"]}}'}
		with self.respond(answers), self.online():
			threads.handle(self.pr, comment)
		asked = next(r for r in self.requests if r.purpose == "missed")
		self.assertIn("def update_user_email(user, email, notify):", str(asked.messages))
		missed = db.value("Review Feedback", {"kind": "Missed", "provider_ref": "h1"}, "name")
		self.assertTrue(missed)
		saved = self.memory(CHECK) or {}
		self.assertEqual(saved.get("path_scope"), "app/**")
		self.assertEqual(frappe.parse_json(saved.get("evidence")), [missed])

	def test_missed_issue_needs_a_review_and_another_reviewer(self):
		answers = {"missed": f'{{"missed": true, "memory": "{CHECK}", "path_scope": []}}'}
		comment = ProviderComment(
			"h2", "inline", "bob", "This crashes on None.", path="app/models.py", line=5
		)
		with self.respond(answers), self.online():
			threads.handle(self.pr, comment)  # no review yet
			self.review([[("submit_review", {"summary": "s", "confidence_score": 5})]])
			threads.handle(
				self.pr,
				ProviderComment("h3", "inline", "ada", "Note: temporary.", path="app/models.py", line=5),
			)
			threads.handle(
				self.pr,
				ProviderComment(
					"h4", "inline", "bob", "Agreed.", path="app/models.py", line=5, in_reply_to="h9"
				),
			)
		self.assertFalse([r for r in self.requests if r.purpose == "missed"])
		self.assertFalse(db.value("Review Feedback", {"kind": "Missed", "pull_request": self.pr}, "name"))


class TestGatewayRetries(LearningCase):
	"""Comments and addressed checks wait for a busy or failing gateway, and then run again."""

	def question(self) -> ProviderComment:
		_, comment_id = self.posted_finding()
		# Rows outlive a test: Frappe rolls back once for each class. So each test gets its own id.
		reply = ProviderComment(
			frappe.generate_hash(length=8),
			"inline",
			"bob",
			"Why is this a problem?",
			in_reply_to=comment_id,
			number=7,
		)
		handler.store_comment(self.pr, self.repository, reply, False)
		return reply

	def busy_answers(self, busy: bool):
		"""Read the reply as a question. The answer fails while `busy` is true."""
		responder = scripted(
			["The cached copy is shared."], {"reply": '{"class": "answer", "sentiment": "Question"}'}
		)

		def respond(request):
			if busy and request.purpose == "conversation":
				raise GatewayBusy("429", 5)
			return responder(request)

		return use_responder(respond)

	def comment_retry(self, comment_id):
		return (
			db.values(
				"Pull Request Comment",
				{"pull_request": self.pr, "provider_comment_id": comment_id},
				("attempt", "retry_at"),
			)
			or {}
		)

	def reactions(self, comment_id):
		return [r[2] for r in self.adapter.named("reaction") if r[1] == comment_id]

	def test_answer_waits_for_a_busy_gateway_and_runs_again(self):
		reply = self.question()
		with self.busy_answers(True), self.online():
			handler.handle_comment(self.pr, reply.__dict__)
		retry = self.comment_retry(reply.id)
		self.assertEqual(retry["attempt"], 1)
		self.assertGreater(retry["retry_at"], now_datetime())
		self.assertEqual(self.reactions(reply.id), ["eyes"])  # no 😕 while Guard still tries
		self.assertFalse(self.adapter.named("reply"))

		db.set_values(
			"Pull Request Comment",
			str(
				db.value(
					"Pull Request Comment", {"pull_request": self.pr, "provider_comment_id": reply.id}, "name"
				)
			),
			{"retry_at": add_to_date(now_datetime(), seconds=-1)},
		)
		with self.busy_answers(False), self.online():
			handler.resume_comments()
		self.assertEqual(self.adapter.named("reply"), ["The cached copy is shared."])
		self.assertIn("+1", self.reactions(reply.id))
		self.assertNotIn("confused", self.reactions(reply.id))
		self.assertIsNone(self.comment_retry(reply.id)["retry_at"])
		# The retry did not record the reply a second time.
		self.assertEqual(frappe.db.count("Review Feedback", {"provider_ref": reply.id}), 1)

	def test_resume_skips_rows_without_a_retry_time(self):
		reply = self.question()
		db.set_values("Pull Request", self.pr, {"state": "closed", "addressed_retry_at": None})
		review = frappe.get_doc(
			{
				"doctype": "Code Review",
				"pull_request": self.pr,
				"repository": self.repository,
				"source": "pull_request",
				"status": "Queued",
				"trigger": "open",
			}
		).insert(ignore_permissions=True)
		with (
			patch("guard.webhooks.handler.enqueue_comment") as comments,
			patch("guard.learning.addressed.enqueue") as checks,
			patch("guard.review.jobs.enqueue_review") as reviews,
		):
			handler.resume_comments()
			addressed.resume()
			jobs.resume_waiting()
		self.assertNotIn(reply.id, [call.args[1].id for call in comments.call_args_list])
		self.assertNotIn(self.pr, [call.kwargs["pull_request"] for call in checks.call_args_list])
		self.assertNotIn(review.name, [call.args[0] for call in reviews.call_args_list])

	def test_answer_gives_up_after_the_last_attempt(self):
		reply = self.question()
		last = len(handler.COMMENT_RETRY_DELAYS)
		with self.busy_answers(True), self.online(), self.assertRaises(GatewayBusy):
			handler.handle_comment(self.pr, reply.__dict__, attempt=last)
		self.assertIsNone(self.comment_retry(reply.id)["retry_at"])
		self.assertEqual(self.reactions(reply.id)[-2:], ["removed", "confused"])

	def test_close_check_waits_for_a_busy_gateway(self):
		with patch("guard.learning.addressed._judge_close", side_effect=GatewayBusy("429", 5)):
			addressed.on_close(self.pr)
		row = db.values("Pull Request", self.pr, ("addressed_attempt", "addressed_retry_at")) or {}
		self.assertEqual(row["addressed_attempt"], 1)
		self.assertGreater(row["addressed_retry_at"], now_datetime())

		db.set_values(
			"Pull Request",
			self.pr,
			{"state": "closed", "addressed_retry_at": add_to_date(now_datetime(), seconds=-1)},
		)
		with patch("guard.learning.addressed._judge_close") as judge:
			addressed.resume()
		judge.assert_called_once_with(self.pr)
		self.assertIsNone(db.value("Pull Request", self.pr, "addressed_retry_at"))

	def test_push_check_runs_again_from_the_first_push_that_failed(self):
		with patch("guard.learning.addressed._judge_push", side_effect=GatewayBusy("429", 5)):
			addressed.on_push(self.pr, "first", "second")
			addressed.on_push(self.pr, "second", "third")
		self.assertEqual(db.value("Pull Request", self.pr, "addressed_since"), "first")

		head = db.value("Pull Request", self.pr, "head_sha")
		frappe.db.set_value(
			"Pull Request", self.pr, "addressed_retry_at", add_to_date(now_datetime(), seconds=-1)
		)
		with patch("guard.learning.addressed._judge_push") as judge:
			addressed.resume()
		judge.assert_called_once_with(self.pr, "first", head)
		self.assertFalse(db.value("Pull Request", self.pr, "addressed_since"))

	def test_check_fails_after_the_last_attempt(self):
		last = len(addressed.RETRY_DELAYS)
		with (
			patch("guard.learning.addressed._judge_close", side_effect=GatewayBusy("429", 5)),
			self.assertRaises(GatewayBusy),
		):
			addressed.on_close(self.pr, attempt=last)
		self.assertIsNone(db.value("Pull Request", self.pr, "addressed_retry_at"))

	def test_comment_and_addressed_jobs_get_a_long_timeout(self):
		with patch("frappe.enqueue") as enqueue:
			handler.enqueue_comment(self.pr, ProviderComment("c1", "issue", "bob", "hi"))
			addressed.enqueue("on_close", pull_request=self.pr)
		self.assertEqual([c.kwargs["timeout"] for c in enqueue.call_args_list], [1800, 1800])


class TestResolvedThreads(LearningCase):
	"""A person resolves the thread of a finding to close it without a fix."""

	def thread_event(self, thread_id: str, action: str, sender: str):
		payload = {
			"action": action,
			"thread": {
				"node_id": "PRRT_1",
				"comments": [
					{"id": int(thread_id), "in_reply_to_id": None},
					{"id": 9, "in_reply_to_id": int(thread_id)},
				],
			},
			"pull_request": {"number": 7},
			"sender": {"login": sender},
		}
		[event] = github.parse("pull_request_review_thread", payload)
		return event

	def resolve(self, thread_id: str, sender: str, action: str = "resolved") -> bool:
		return handler._thread(self.repository, self.thread_event(thread_id, action, sender))

	def status(self, finding: str) -> tuple[str, str]:
		row = db.values("Review Finding", finding, ("status", "status_reason")) or {}
		return row["status"], row["status_reason"]

	def test_resolving_the_thread_dismisses_the_finding_and_reopening_restores_it(self):
		finding, thread_id = self.posted_finding()
		self.assertTrue(self.resolve(thread_id, "ada"))
		self.assertEqual(self.status(finding), ("Dismissed", "thread resolved by ada"))
		# The review scored 3/5 for the P1. With no finding open, the check passes.
		self.assertEqual(self.adapter.checks[-1][1:3], ("success", "5/5 · 0 open finding(s)"))
		# The thread is already resolved on the provider: Guard does not resolve it again.
		self.assertFalse(self.adapter.named("resolve"))
		self.assertTrue(self.resolve(thread_id, "ada", "unresolved"))
		self.assertEqual(self.status(finding), ("Open", "thread reopened by ada"))
		self.assertEqual(self.adapter.checks[-1][1:3], ("failure", "3/5 · 1 open finding(s)"))
		self.assertFalse(self.adapter.named("unresolve"))

	def test_guard_and_people_without_teach_permission_do_not_close_findings(self):
		finding, thread_id = self.posted_finding()
		frappe.db.set_single_value("Guard Settings", "bot_logins", "guard-bot")
		self.assertFalse(self.resolve(thread_id, "guard-bot"))
		self.assertFalse(self.adapter.named("unresolve"))
		frappe.db.set_single_value("Guard Settings", "teach_permission", "Guard users")
		self.assertTrue(self.resolve(thread_id, "stranger"))
		self.assertEqual(self.status(finding)[0], "Open")
		# The provider must not show the thread closed while the finding still fails the check.
		self.assertEqual(self.adapter.named("unresolve"), [thread_id])
		[reply] = self.adapter.named("reply")
		self.assertIn("@stranger, only people with a Guard account can close", reply)

	def test_a_collaborator_with_write_access_closes_findings_as_a_member_does(self):
		finding, thread_id = self.posted_finding()
		frappe.db.set_single_value("Guard Settings", "teach_permission", "Organization members")
		with patch.object(self.adapter, "is_member", return_value=False):
			with patch.object(self.adapter, "can_write", return_value=True):
				self.assertTrue(self.resolve(thread_id, "outside-maintainer"))
		self.assertEqual(self.status(finding), ("Dismissed", "thread resolved by outside-maintainer"))
		self.assertFalse(self.adapter.named("unresolve"))

	def test_a_resolve_by_a_reader_names_members_and_writers(self):
		finding, thread_id = self.posted_finding()
		frappe.db.set_single_value("Guard Settings", "teach_permission", "Organization members")
		with patch.object(self.adapter, "is_member", return_value=False):
			self.assertTrue(self.resolve(thread_id, "reader"))
		self.assertEqual(self.status(finding)[0], "Open")
		[reply] = self.adapter.named("reply")
		self.assertIn("only members of the acme organization and people with write access to acme/", reply)

	def test_a_resolve_that_the_provider_cannot_check_reopens_the_thread_without_a_no(self):
		finding, thread_id = self.posted_finding()
		frappe.db.set_single_value("Guard Settings", "teach_permission", "Organization members")
		with patch.object(self.adapter, "is_member", side_effect=ProviderError("rate limited", 403)):
			self.assertTrue(self.resolve(thread_id, "maybe-member"))
		self.assertEqual(self.status(finding)[0], "Open")
		# A webhook comes once: a thread left resolved would stay closed on an open finding.
		self.assertEqual(self.adapter.named("unresolve"), [thread_id])
		[reply] = self.adapter.named("reply")
		self.assertIn("@maybe-member, I could not check", reply)
		self.assertNotIn("only members", reply)
		self.assertIsNone(cache.get("guard:member:acme:maybe-member"))

	def test_a_repository_without_a_provider_caches_no_permission_answer(self):
		frappe.db.set_single_value("Guard Settings", "teach_permission", "Organization members")
		with patch("guard.review.permissions.adapter_for_repository", return_value=None):
			# No answer, not a no: a resolve gets the reply for an unchecked permission.
			with self.assertRaises(ProviderError):
				permissions.passes("teach", "no-provider", self.repository)
			self.assertFalse(permissions.allowed("teach", "no-provider", self.repository))
		self.assertIsNone(cache.get("guard:member:acme:no-provider"))

	def test_reopening_a_thread_does_not_reopen_a_finding_closed_another_way(self):
		finding, thread_id = self.posted_finding()
		ui.set_finding_status(finding, "Dismissed")
		self.assertFalse(self.resolve(thread_id, "ada", "unresolved"))
		self.assertEqual(self.status(finding)[0], "Dismissed")

	def test_polling_reads_resolved_threads(self):
		finding, thread_id = self.posted_finding()
		self.adapter.threads = [ReviewThread(thread_id, resolved=True, resolved_by="ada")]
		polling._poll_threads(self.repository, "acme/app", 7, self.adapter)
		self.assertEqual(self.status(finding), ("Dismissed", "thread resolved by ada"))
		self.adapter.threads = [ReviewThread(thread_id, resolved=False)]
		polling._poll_threads(self.repository, "acme/app", 7, self.adapter)
		self.assertEqual(self.status(finding), ("Open", "thread reopened"))

	def test_polling_reopens_a_thread_that_nobody_known_resolved(self):
		finding, thread_id = self.posted_finding()
		self.adapter.threads = [ReviewThread(thread_id, resolved=True, resolved_by="")]
		polling._poll_threads(self.repository, "acme/app", 7, self.adapter)
		self.assertEqual(self.status(finding)[0], "Open")
		self.assertEqual(self.adapter.named("unresolve"), [thread_id])
		[reply] = self.adapter.named("reply")
		self.assertTrue(reply.startswith("I could not tell who resolved this thread"))
