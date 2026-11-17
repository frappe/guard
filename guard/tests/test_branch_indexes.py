from unittest.mock import PropertyMock, patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import now_datetime

from guard.config import dashboard
from guard.git.repository import store_for
from guard.git.store import GitError, GitStore
from guard.indexing import branches, jobs
from guard.indexing.indexer import Indexer, index_lock
from guard.llm.client import LLMClient, use_responder
from guard.review import trigger
from guard.tests.cases import PullRequestCase
from guard.tests.fakes import scripted
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db
from guard.webhooks import handler
from guard.webhooks.events import WebhookEvent

HOTFIX = "version-15-hotfix"
# A caller that only the release branch has.
HOTFIX_CALLER = """from app.models import update_user_email


def hotfix_email(user):
    return update_user_email(user, "fixed@example.com")
"""


PATTERNS = ["version-*-hotfix"]


def listed_config() -> str:
	return 'index_branches = ["version-*-hotfix"]\n'


class TestBaseBranchReview(PullRequestCase):
	"""Pull request #7 goes to the release branch `version-15-hotfix`, not to the default branch."""

	def setUp(self):
		super().setUp()
		self.repo.git("checkout", "-q", "-b", HOTFIX, self.base)
		self.hotfix = self.repo.commit({"app/hotfix.py": HOTFIX_CALLER}, "a caller on the release branch")
		self.repo.git("checkout", "-q", "feature")
		frappe.db.set_value("Pull Request", self.pr, "base_branch", HOTFIX)

	def commit_on_main(self, files):
		self.repo.git("checkout", "-q", "main")
		head = self.repo.commit(files, "on main")
		self.repo.git("checkout", "-q", "feature")
		return head

	def review_task(self) -> str:
		"""Review the pull request with the clients online, and return the task of the agent."""
		requests = []
		respond = scripted([[("submit_review", {"summary": "s", "confidence_score": 5})]])

		def record(request):
			requests.append(request)
			return respond(request)

		with (
			patch.object(LLMClient, "online", new_callable=PropertyMock, return_value=True),
			use_responder(record),
		):
			trigger.consider(self.pr, "open")
		return next(r for r in requests if r.purpose == "review").messages[1].content

	def branch_index(self) -> str:
		return str(
			db.value("Repository Index", {"repository": self.repository, "branch": HOTFIX}, "name") or ""
		)

	def test_the_first_review_queues_the_branch_index_and_uses_the_default_graph(self):
		main = self.commit_on_main({".guard/config.toml": listed_config()})
		with patch("guard.indexing.jobs.enqueue_index") as enqueue:
			task = self.review_task()
		# The first index of a branch is a full index: the review does not wait for it.
		self.assertEqual([c.args for c in enqueue.call_args_list], [(self.repository, HOTFIX)])
		self.assertEqual(
			db.values("Repository Index", self.branch_index(), ("auto", "status")),
			{"auto": 1, "status": "submitted"},
		)
		self.assertIn(f"The code graph is of commit {main[:12]} of the default branch `main`", task)
		self.assertNotIn("app/hotfix.py", task)

	def test_a_listed_base_branch_reviews_with_its_own_index_when_it_is_completed(self):
		self.commit_on_main({".guard/config.toml": listed_config()})
		# Tests run queued jobs at once: this makes and completes the index.
		self.assertIsNone(branches.ready_index(self.repository, HOTFIX, PATTERNS, queue=True))
		index = self.branch_index()
		self.assertEqual(
			db.values("Repository Index", index, ("status", "sha")),
			{"status": "completed", "sha": self.hotfix},
		)
		task = self.review_task()
		# The caller of the release branch is at risk: the graph is of that branch.
		self.assertIn("app/hotfix.py", task)
		self.assertNotIn("of the default branch", task)
		# The default index of the repository stays the one of the default branch.
		self.assertNotEqual(db.value("Code Repository", self.repository, "default_index"), index)

	def test_an_unlisted_base_branch_gets_the_default_index_and_a_note(self):
		main = self.commit_on_main({"app/other.py": "X = 1\n"})
		task = self.review_task()
		self.assertFalse(self.branch_index())
		self.assertNotIn("app/hotfix.py", task)
		self.assertIn(f"The code graph is of commit {main[:12]} of the default branch `main`", task)

	def test_a_branch_past_the_limit_gets_the_default_index_and_a_note(self):
		self.commit_on_main({".guard/config.toml": listed_config()})
		for n in range(1, branches.MAX_BRANCH_INDEXES + 1):
			self.assertTrue(branches.index_of(self.repository, f"version-{n}-hotfix", PATTERNS))
		with patch("guard.indexing.jobs.enqueue_index") as enqueue:
			task = self.review_task()
		self.assertFalse(self.branch_index())
		enqueue.assert_not_called()
		self.assertIn("of the default branch `main`", task)

	def test_a_failed_branch_index_gets_the_default_index_and_a_note(self):
		self.commit_on_main({".guard/config.toml": listed_config()})
		index = str(branches.index_of(self.repository, HOTFIX, PATTERNS))
		db.set_values(
			"Repository Index",
			index,
			{"status": "failed", "error": "parser crashed", "completed_at": now_datetime()},
		)
		with patch("guard.indexing.jobs.enqueue_index") as enqueue:
			task = self.review_task()
		enqueue.assert_not_called()  # A failed run blocks indexing for an hour.
		self.assertIn("of the default branch `main`", task)
		self.assertNotIn("app/hotfix.py", task)

	def test_a_drop_waits_for_the_reviews_of_the_branch(self):
		self.commit_on_main({".guard/config.toml": listed_config()})
		branches.ready_index(self.repository, HOTFIX, PATTERNS, queue=True)
		index = self.branch_index()
		review = frappe.get_doc(
			{
				"doctype": "Code Review",
				"pull_request": self.pr,
				"repository": self.repository,
				"status": "Queued",
			}
		).insert(ignore_permissions=True)
		self.assertFalse(branches.drop_index(index), "a queued review can read it")
		frappe.db.set_value("Code Review", str(review.name), "status", "Completed")
		self.assertTrue(branches.drop_index(index))

	def test_a_pull_request_to_the_default_branch_has_no_note(self):
		frappe.db.set_value("Pull Request", self.pr, "base_branch", "main")
		self.assertNotIn("of the default branch", self.review_task())


class TestBranchIndexJobs(IntegrationTestCase):
	"""A repository with the default branch `main` and the release branch `version-15-hotfix`."""

	def setUp(self):
		self.repo = TempRepo({"app/__init__.py": "", "app/models.py": "def f():\n    return 1\n"})
		self.addCleanup(self.repo.cleanup)
		self.repository = make_repository(self.repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		self.repo.git("checkout", "-q", "-b", HOTFIX)
		self.repo.commit({"app/hotfix.py": "from app.models import f\n\n\ndef g():\n    return f()\n"}, "fix")
		self.repo.git("checkout", "-q", "main")
		# Only this repository: the site can hold others whose clone URL is on GitHub.
		for target, value in (
			("guard.indexing.jobs.store_for", self.store_for),
			("guard.indexing.branches.store_for", self.store_for),
			("guard.indexing.jobs.frappe.log_error", None),
			("guard.indexing.branches.frappe.log_error", None),
		):
			patcher = patch(target, value) if value else patch(target)
			patcher.start()
			self.addCleanup(patcher.stop)

	def store_for(self, repository: str) -> GitStore:
		if repository != self.repository:
			raise GitError("not this test")
		return store_for(repository)

	def set_config(self, text: str | None):
		self.repo.commit({".guard/config.toml": text}, "config")

	def push(self, branch: str, after: str = "1" * 40) -> bool:
		with patch("guard.indexing.jobs.enqueue_index") as enqueue:
			handled = handler._push(self.repository, WebhookEvent("push", ref=branch, after=after))
		self.queued = [c.args[1:] for c in enqueue.call_args_list]
		return handled

	def test_a_push_to_a_listed_branch_queues_its_index(self):
		self.set_config(listed_config())
		self.assertTrue(self.push(HOTFIX))
		self.assertEqual(self.queued, [(HOTFIX,)])
		self.assertEqual(
			db.value("Repository Index", {"repository": self.repository, "branch": HOTFIX}, "auto"), 1
		)

	def test_a_push_to_an_unlisted_branch_queues_nothing(self):
		self.set_config(listed_config())
		self.assertFalse(self.push("feature"))
		self.assertFalse(self.push(HOTFIX, after="0" * 40), "a deleted branch")
		self.assertFalse(self.push("refs/tags/v15.1.0"))
		self.assertEqual(self.queued, [])

	def test_without_the_setting_a_push_queues_nothing(self):
		self.assertFalse(self.push(HOTFIX))
		self.assertEqual(self.queued, [])

	def test_the_dashboard_lists_branches_too(self):
		doc = frappe._dict(name="Organization", index_branches="version-*-hotfix\n\nversion-14\n")
		self.assertEqual(
			dashboard.settings_layer(doc).settings["index_branches"], ["version-*-hotfix", "version-14"]
		)

	def index_both(self, auto: bool = True) -> tuple[str, str]:
		self.set_config(listed_config())
		default = jobs.ensure_fresh(self.repository)
		if auto:
			self.assertTrue(branches.index_of(self.repository, HOTFIX, PATTERNS))
		else:
			jobs.index_for(self.repository, HOTFIX)  # as the query API makes it
		branch = jobs.ensure_fresh(self.repository, branch=HOTFIX)
		self.assertNotEqual(default, branch)
		self.assertEqual(db.value("Repository Index", branch, "auto"), int(auto))
		return default, branch

	def counts(self, index: str) -> tuple[int, int, int, int]:
		def count(table: str) -> int:
			return int(
				db.scalar(f"SELECT COUNT(*) FROM `{table}` WHERE `index` = %s", (index,))
			)  # nosemgrep: frappe-sql-format-injection

		vectors = int(
			db.scalar(
				"""SELECT COUNT(*) FROM `guard_vector` v JOIN `tabCode Chunk` c ON c.name = v.ref
				WHERE c.`index` = %s""",
				(index,),
			)
		)
		return count("tabCode Entity"), count("tabCode Edge"), count("tabCode Chunk"), vectors

	def test_catch_up_drops_the_index_of_a_branch_no_longer_listed(self):
		default, branch = self.index_both()
		before = self.counts(branch)
		self.assertTrue(all(before), before)
		chunks = [r[0] for r in db.rows("SELECT name FROM `tabCode Chunk` WHERE `index` = %s", (branch,))]
		self.set_config(None)
		# Small batches: the drop deletes in several rounds.
		with patch.object(branches, "DROP_BATCH", 2):
			jobs.catch_up()
		self.assertFalse(frappe.db.exists("Repository Index", branch))
		self.assertEqual(self.counts(branch), (0, 0, 0, 0))
		self.assertFalse(
			db.scalar(
				"SELECT COUNT(*) FROM `guard_vector` WHERE kind = 'chunk' AND ref IN %s", (tuple(chunks),)
			)
		)
		self.assertTrue(all(self.counts(default)), "the default index stays")
		self.assertTrue(frappe.db.exists("Repository Index", default))
		# A job queued before the drop finds no index, and stops.
		jobs.run_index(branch)

	def test_catch_up_drops_the_index_of_a_deleted_branch(self):
		_, branch = self.index_both()
		self.repo.git("branch", "-D", HOTFIX)
		jobs.catch_up()
		self.assertFalse(frappe.db.exists("Repository Index", branch))

	def test_an_index_of_the_query_api_stays_until_its_branch_is_gone(self):
		_, branch = self.index_both(auto=False)
		self.set_config(None)
		jobs.catch_up()
		self.assertTrue(frappe.db.exists("Repository Index", branch), "the setting did not make it")
		self.assertTrue(all(self.counts(branch)))
		self.repo.git("branch", "-D", HOTFIX)
		jobs.catch_up()
		self.assertFalse(frappe.db.exists("Repository Index", branch))

	def test_a_drop_skips_a_locked_or_queued_index(self):
		_, branch = self.index_both()
		with index_lock(branch):
			self.assertFalse(branches.drop_index(branch), "a run writes it")
		frappe.db.set_value("Repository Index", branch, "status", "submitted")
		self.assertFalse(branches.drop_index(branch), "a job is queued")
		self.assertEqual(db.value("Repository Index", branch, "status"), "submitted")
		self.assertTrue(all(self.counts(branch)))
		frappe.db.set_value("Repository Index", branch, "status", "failed")
		self.assertTrue(branches.drop_index(branch))

	def test_catch_up_goes_on_after_a_repository_fails(self):
		with (
			patch.object(branches, "_catch_up", side_effect=[RuntimeError("boom"), None]) as catch_up,
			patch.object(
				branches,
				"branch_indexes",
				return_value=[branches.BranchIndex("x", "b", True, "completed", "")],
			),
			patch.object(db, "rows", return_value=[("one",), ("two",)]),
		):
			branches.catch_up()
		self.assertEqual([c.args[0] for c in catch_up.call_args_list], ["one", "two"])

	def test_the_limit_counts_only_listed_indexes_that_guard_made_and_the_oldest_win(self):
		api = jobs.index_for(self.repository, "version-0-hotfix")  # not marked: does not count
		jobs.index_for(self.repository, "version-0-other", auto=True)  # not listed: does not count
		made = [branches.index_of(self.repository, f"version-{n}-hotfix", PATTERNS) for n in range(1, 6)]
		self.assertTrue(all(made))
		self.assertIsNone(branches.index_of(self.repository, "version-6-hotfix", PATTERNS))
		self.assertEqual(branches.index_of(self.repository, "version-0-hotfix", PATTERNS), api)
		# A wider setting can leave more marked indexes than the limit. The oldest win.
		jobs.index_for(self.repository, "version-7-hotfix", auto=True)
		self.assertIsNone(branches.index_of(self.repository, "version-7-hotfix", PATTERNS))
		self.assertEqual(branches.index_of(self.repository, "version-1-hotfix", PATTERNS), made[0])

	def test_branch_names_match_without_case(self):
		index = branches.index_of(self.repository, HOTFIX, PATTERNS)
		self.assertEqual(branches.index_of(self.repository, HOTFIX.upper(), PATTERNS), index)
		self.assertIsNone(branches.index_of(self.repository, "MAIN", ["*"]), "the default branch")

	def test_a_failed_branch_index_does_not_fail_the_repository(self):
		_, branch = self.index_both()
		status = db.value("Code Repository", self.repository, "index_status")
		with (
			patch.object(Indexer, "_run", side_effect=RuntimeError("parser crashed")),
			self.assertRaises(RuntimeError),
		):
			jobs.run_index(branch, full=True)
		self.assertEqual(db.value("Repository Index", branch, "status"), "failed")
		self.assertEqual(db.value("Code Repository", self.repository, "index_status"), status)

	def test_catch_up_indexes_a_listed_branch_that_moved(self):
		_, branch = self.index_both()
		self.repo.git("checkout", "-q", HOTFIX)
		head = self.repo.commit({"app/more.py": "Y = 2\n"}, "missed push")
		self.repo.git("checkout", "-q", "main")
		jobs.catch_up()
		self.assertEqual(db.value("Repository Index", branch, "sha"), head)

	def test_the_default_index_is_never_dropped(self):
		default, _ = self.index_both()
		self.assertFalse(branches.drop_index(default))
		self.assertTrue(all(self.counts(default)))
