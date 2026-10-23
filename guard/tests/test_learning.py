from unittest.mock import patch

import frappe

from guard.api import ui
from guard.conversation import threads
from guard.indexing.vectors import VectorRow, VectorStore
from guard.learning import addressed, feedback, memories, memory_writer
from guard.llm.client import ChatRequest, use_responder
from guard.llm.embeddings import Embedder
from guard.providers.base import ProviderComment, Reaction
from guard.review import trigger
from guard.tests.cases import PullRequestCase
from guard.tests.fakes import scripted
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db

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


class TestLearning(PullRequestCase):
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

	def test_ack_reply_marks_addressed_and_reacts(self):
		finding, comment_id = self.posted_finding()
		reply = ProviderComment("r2", "inline", "ada", "Good catch, fixed.", in_reply_to=comment_id, number=7)
		with self.respond({"reply": '{"class": "ack", "sentiment": "Agree"}'}), self.online():
			threads.handle(self.pr, reply)
		self.assertEqual(db.value("Review Finding", finding, "status"), "Addressed")
		self.assertIn(("inline", "r2", "+1"), [r[:3] for r in self.adapter.named("reaction")])
		self.assertEqual(len([r for r in self.requests if r.purpose == "reply"]), 1)

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
