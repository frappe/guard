from unittest.mock import PropertyMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from guard.config import dashboard
from guard.git.repository import store_for
from guard.git.store import GitError, GitStore
from guard.indexing import branches, jobs
from guard.indexing.indexer import Indexer
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

	def test_a_listed_base_branch_reviews_with_its_own_index(self):
		self.commit_on_main({".guard/config.toml": listed_config()})
		task = self.review_task()
		index = self.branch_index()
		self.assertTrue(index, "the first review of the branch makes its index")
		self.assertEqual(
			db.values("Repository Index", index, ("status", "sha")),
			{"status": "completed", "sha": self.hotfix},
		)
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
		with patch.object(branches, "MAX_BRANCH_INDEXES", 0):
			task = self.review_task()
		self.assertFalse(self.branch_index())
		self.assertIn("of the default branch `main`", task)

	def test_a_failed_branch_index_gets_the_default_index_and_a_note(self):
		self.commit_on_main({".guard/config.toml": listed_config()})
		run = Indexer._run

		def fail_on_the_branch(indexer):
			if indexer.doc.branch == HOTFIX:
				raise RuntimeError("parser crashed")
			return run(indexer)

		with patch.object(Indexer, "_run", autospec=True, side_effect=fail_on_the_branch):
			task = self.review_task()
		self.assertIn("of the default branch `main`", task)
		self.assertNotIn("app/hotfix.py", task)
		self.assertEqual(db.value("Repository Index", self.branch_index(), "status"), "failed")
		# The status of the repository is about the default branch only.
		self.assertEqual(db.value("Code Repository", self.repository, "index_status"), "Indexed")

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

	def index_both(self) -> tuple[str, str]:
		self.set_config(listed_config())
		default = jobs.ensure_fresh(self.repository)
		branch = jobs.ensure_fresh(self.repository, branch=HOTFIX)
		self.assertNotEqual(default, branch)
		return default, branch

	def counts(self, index: str) -> tuple[int, int]:
		entities = int(db.scalar("SELECT COUNT(*) FROM `tabCode Entity` WHERE `index` = %s", (index,)))
		vectors = int(
			db.scalar(
				"""SELECT COUNT(*) FROM `guard_vector` v JOIN `tabCode Chunk` c ON c.name = v.ref
				WHERE c.`index` = %s""",
				(index,),
			)
		)
		return entities, vectors

	def test_catch_up_drops_the_index_of_a_branch_no_longer_listed(self):
		default, branch = self.index_both()
		self.assertTrue(all(self.counts(branch)))
		self.set_config(None)
		jobs.catch_up()
		self.assertFalse(frappe.db.exists("Repository Index", branch))
		self.assertEqual(self.counts(branch), (0, 0))
		self.assertTrue(all(self.counts(default)), "the default index stays")
		self.assertTrue(frappe.db.exists("Repository Index", default))

	def test_catch_up_drops_the_index_of_a_deleted_branch(self):
		_, branch = self.index_both()
		self.repo.git("branch", "-D", HOTFIX)
		jobs.catch_up()
		self.assertFalse(frappe.db.exists("Repository Index", branch))

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
