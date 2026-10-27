from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from guard.patches.merge_duplicate_pull_requests import merge
from guard.providers.base import PullRequestInfo
from guard.review import pull_requests
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db


def info(number: int, title: str = "Change") -> PullRequestInfo:
	return PullRequestInfo(number, title, "", "ada", "open", False, "main", "feature", "", "", "https://x")


class TestPullRequests(IntegrationTestCase):
	def setUp(self):
		self.repo = TempRepo({"app.py": ""})
		self.repository = make_repository(self.repo, full_name=f"acme/{frappe.generate_hash(length=6)}")

	def tearDown(self):
		self.repo.cleanup()

	def test_upsert_updates_a_pull_request_that_another_job_inserted(self):
		name = str(pull_requests.upsert(self.repository, info(7)).name)
		frappe.clear_messages()
		find = pull_requests.find
		# The first lookup ran before the other job inserted the row.
		answers = iter([None])
		with patch.object(
			pull_requests, "find", side_effect=lambda *args, **kwargs: next(answers, find(*args, **kwargs))
		):
			doc = pull_requests.upsert(self.repository, info(7, "Renamed"))
		self.assertEqual(doc.name, name)
		rows = frappe.get_all("Pull Request", filters={"repository": self.repository}, fields=["title"])
		self.assertEqual([row.title for row in rows], ["Renamed"])
		self.assertFalse(frappe.local.message_log)

	def test_a_repository_has_one_row_for_each_pull_request_number(self):
		pull_requests.upsert(self.repository, info(7))
		duplicate = frappe.new_doc("Pull Request")
		duplicate.update({"repository": self.repository, "number": 7, "title": "Change"})
		with self.assertRaises(frappe.UniqueValidationError):
			duplicate.insert(ignore_permissions=True)

	def row(self, number: int, seconds: int, values: dict | None = None) -> str:
		"""A pull request row that changed `seconds` from now. The number stands in for a duplicate:
		the unique index stops real ones."""
		name = str(pull_requests.upsert(self.repository, info(number)).name)
		if values:
			db.set_values("Pull Request", name, values)
		frappe.db.set_value(
			"Pull Request",
			name,
			"modified",
			add_to_date(now_datetime(), seconds=seconds),
			update_modified=False,
		)
		return name

	def review(self, pull_request: str, status: str = "Completed", minutes: int = 0) -> str:
		doc = frappe.get_doc(
			{
				"doctype": "Code Review",
				"pull_request": pull_request,
				"repository": self.repository,
				"status": status,
				"completed_at": add_to_date(now_datetime(), minutes=minutes)
				if status == "Completed"
				else None,
			}
		).insert(ignore_permissions=True)
		return str(doc.name)

	def test_merge_keeps_the_review_state_when_a_later_event_changed_another_row(self):
		keep = self.row(7, 0, {"summary_comment_id": "11", "last_reviewed_sha": "abc", "guard_approved": 1})
		reviewed = self.review(keep)
		labeled = self.row(8, 10, {"title": "Latest"})
		addressed = self.row(9, 5, {"addressed_since": "def"})
		self.review(addressed, "Cancelled")

		merge(keep, [labeled, addressed])

		self.assertEqual(
			db.values(
				"Pull Request",
				keep,
				(
					"number",
					"title",
					"summary_comment_id",
					"last_reviewed_sha",
					"guard_approved",
					"addressed_since",
					"latest_review",
					"review_count",
				),
			),
			{
				"number": 7,
				"title": "Latest",
				"summary_comment_id": "11",
				"last_reviewed_sha": "abc",
				"guard_approved": 1,
				"addressed_since": "def",
				"latest_review": reviewed,
				"review_count": 1,
			},
		)
		self.assertEqual(frappe.db.count("Code Review", {"pull_request": keep}), 2)
		for name in (labeled, addressed):
			self.assertFalse(frappe.db.exists("Pull Request", name))

	def test_merge_moves_the_newest_review_and_all_links_to_the_oldest_row(self):
		keep = self.row(7, 10, {"summary_comment_id": "11"})
		self.review(keep, minutes=-30)
		duplicate = self.row(8, 0, {"summary_comment_id": "42", "last_reviewed_sha": "abc"})
		newest = self.review(duplicate)
		frappe.get_doc({"doctype": "Pull Request Comment", "pull_request": duplicate}).insert(
			ignore_permissions=True
		)

		merge(keep, [duplicate])

		self.assertTrue(
			frappe.db.exists(
				"Deleted Document", {"deleted_doctype": "Pull Request", "deleted_name": duplicate}
			)
		)
		self.assertEqual(
			db.values(
				"Pull Request",
				keep,
				("summary_comment_id", "last_reviewed_sha", "latest_review", "review_count"),
			),
			{
				"summary_comment_id": "42",
				"last_reviewed_sha": "abc",
				"latest_review": newest,
				"review_count": 2,
			},
		)
		self.assertEqual(frappe.db.count("Pull Request Comment", {"pull_request": keep}), 1)
