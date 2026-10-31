import os
import shutil
import subprocess
import time
from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from guard.git.repository import store_for
from guard.git.store import GitError
from guard.indexing.jobs import ensure_fresh
from guard.llm.client import use_responder
from guard.review import local
from guard.tests.cases import MODELS, SERVICE
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db

NEW_SIGNATURE = MODELS.replace(
	"def update_user_email(user, email):", "def update_user_email(user, email, notify):"
)
# An AWS key, which the secret check finds.
KEY_LINE = 'BACKUP_KEY = "AKIAABCDEFGHIJKLMNOP"\n'


SITE_URL = "http://guard-test.localhost:8000"


class LocalCase(IntegrationTestCase):
	"""A repository with an index, and a change in its working tree."""

	def setUp(self):
		self.repo = TempRepo({"app/__init__.py": "", "app/models.py": MODELS, "app/service.py": SERVICE})
		self.full_name = f"acme/{frappe.generate_hash(length=6)}"
		self.repository = make_repository(self.repo, full_name=self.full_name)
		ensure_fresh(self.repository)
		self.base = self.repo.head()
		self.stack = ExitStack()
		# A local review runs no model and embeds nothing: the agent of the developer pays.
		self.stack.enter_context(use_responder(self.no_model))
		self.stack.enter_context(patch("guard.llm.embeddings.Embedder.embed", side_effect=self.no_model))
		# The upload command and the hook need an https:// or a local address. The name of the test site
		# (`test_site` in CI) must not decide it.
		self.stack.enter_context(patch("guard.settings.get_url", return_value=SITE_URL))

	def tearDown(self):
		self.stack.close()
		self.repo.cleanup()

	def no_model(self, *args, **kwargs):
		raise AssertionError("a local review called a model")

	def working_tree_diff(self) -> str:
		"""Change the signature in the working tree, without a commit, and add a new file."""
		(self.repo.path / "app/models.py").write_text(NEW_SIGNATURE + KEY_LINE)
		(self.repo.path / "app/notes.py").write_text("NOTE = 1\n")
		self.repo.git("add", "-N", "app/notes.py")
		return self.repo.git("diff", "--binary", self.base) + "\n"

	def start(self, diff: str | None = None) -> dict:
		return local.start(
			f"git@github.com:{self.full_name}.git", self.base, diff or self.working_tree_diff(), "feature"
		)


class TestLocalReview(LocalCase):
	def test_apply_writes_the_tree_of_the_base_with_the_patch(self):
		store = store_for(self.repository)
		store.fetch_sha(self.base)
		store = store.scratch(local._objects(frappe.generate_hash(length=16)))
		self.addCleanup(lambda: shutil.rmtree(store.objects or "", ignore_errors=True))
		tree = store.apply(self.base, self.working_tree_diff())
		self.assertEqual(store.show(tree, "app/models.py"), NEW_SIGNATURE + KEY_LINE)
		self.assertEqual(store.show(tree, "app/notes.py"), "NOTE = 1\n")
		self.assertEqual(store.refs("refs/heads/"), ["refs/heads/main"])
		with self.assertRaises(GitError):
			store.apply(
				self.base,
				"diff --git a/app/models.py b/app/models.py\n--- a/app/models.py\n+++ b/app/models.py\n@@ -1,1 +1,1 @@\n-nothing here\n+x\n",
			)

	def test_start_gives_the_graph_the_rules_and_the_checks(self):
		started = self.start()
		self.assertEqual(started["repository"], self.full_name)
		self.assertEqual(started["files"], 2)
		self.assertIn("AWS access key", " ".join(f["title"] for f in started["findings"]))
		guide = started["guide"]
		self.assertIn("update_user_email", guide)
		self.assertIn("app/service.py", guide)
		self.assertIn(started["review_id"], guide)
		self.assertIn("submit_review", guide)
		# Nothing is saved before the agent submits.
		self.assertFalse(frappe.db.exists("Code Review", {"repository": self.repository}))

	def test_submit_saves_a_local_review_and_posts_nothing(self):
		started = self.start()
		line = NEW_SIGNATURE.splitlines().index("def update_user_email(user, email, notify):") + 1
		result = local.submit(
			started["review_id"],
			[
				{
					"path": "app/models.py",
					"start_line": line,
					"end_line": line,
					"severity": "P1",
					"comment_type": "logic",
					"title": "change_email does not pass notify",
					"body": "app/service.py calls update_user_email with two arguments.",
				},
				{
					"path": "app/service.py",
					"start_line": 6,
					"severity": "P1",
					"title": "Caller misses the new argument",
					"body": "change_email calls update_user_email with two arguments.",
				},
				{
					"path": "app/missing.py",
					"start_line": 1,
					"severity": "P2",
					"title": "Not in the change",
					"body": "x",
				},
			],
			summary="- Adds a notify flag.",
			confidence_score=5,
			risk_level="low",
		)
		review = (
			db.values(
				"Code Review",
				result["review"],
				("source", "status", "credits", "confidence_score", "findings_count", "pull_request", "tier"),
			)
			or {}
		)
		self.assertEqual(review["source"], "local")
		self.assertEqual(review["status"], "Completed")
		self.assertEqual(review["credits"], 0)
		self.assertEqual(review["tier"], "local")
		self.assertFalse(review["pull_request"])
		# The open P1 and the secret cap the score of the agent.
		self.assertLess(review["confidence_score"], 5)
		titles = {f["title"] for f in result["kept"]}
		self.assertIn("change_email does not pass notify", titles)
		self.assertTrue(any("AWS access key" in t for t in titles))
		# A finding on an unchanged caller stays. One on a file that does not exist goes.
		self.assertIn("Caller misses the new argument", titles)
		self.assertNotIn("Not in the change", titles)
		self.assertIn("Not in the change", {f["title"] for f in result["dropped"]})
		rows = frappe.get_all("Review Finding", filters={"code_review": result["review"]}, fields=["posted"])
		self.assertEqual(len(rows), len(result["kept"]) + len(result["dropped"]))
		self.assertFalse(any(r.posted for r in rows))

	def test_a_review_links_the_open_pull_request_and_github_reviews_ignore_it(self):
		from guard.api import ui
		from guard.learning import missed
		from guard.providers.base import ProviderComment
		from guard.review import jobs, state, trigger

		pull_request = self.pull_request("feature")
		self.pull_request("feature", number=8, clone_url="https://github.com/someone/fork.git")
		self.pull_request("feature", number=9, state="closed")
		started = self.start()
		name = local.submit(started["review_id"], [])["review"]
		self.assertEqual(db.value("Code Review", name, "pull_request"), pull_request)
		# The pull request has no review of Guard: the local one owns no check and is not the latest.
		frappe.db.set_value("Code Review", name, "head_sha", "a" * 40)
		self.assertFalse(trigger.owns_check(pull_request, "a" * 40))
		self.assertFalse(state.newer_review_of(pull_request, "a" * 40, "2000-01-01"))
		self.assertEqual(state._reviews_of(pull_request, "a" * 40, ""), [])
		# Learning from missed bugs needs a review of Guard, and a local one does not count.
		comment = ProviderComment("1", "inline", "reviewer", "This breaks.", path="app/models.py")
		with (
			patch("guard.learning.feedback.learning_on", return_value=True),
			patch("guard.review.permissions.allowed", return_value=True),
		):
			self.assertFalse(missed.is_candidate(pull_request, self.repository, comment))
		with self.assertRaises(frappe.ValidationError):
			ui.retry_review(name)
		# The local review of the commit does not stop the full review after an outage.
		outage = frappe.get_doc(
			{
				"doctype": "Code Review",
				"pull_request": pull_request,
				"repository": self.repository,
				"status": "Completed",
				"head_sha": "a" * 40,
				"outage_fallback": 1,
			}
		).insert(ignore_permissions=True)
		db.set_values("Code Review", str(outage.name), {"creation": "2000-01-01"})
		db.set_values("Pull Request", pull_request, {"head_sha": "a" * 40, "latest_review": outage.name})
		with patch("guard.review.jobs.create_review", return_value="CR-FULL") as create:
			self.assertEqual(jobs.follow_up(str(outage.name)), "CR-FULL")
		create.assert_called_once()
		# Another branch has no pull request.
		other = local.start(self.full_name, self.base, self.working_tree_diff(), "other")
		self.assertFalse(
			db.value("Code Review", local.submit(other["review_id"], [])["review"], "pull_request")
		)

	def pull_request(
		self, branch: str, number: int = 7, state: str = "open", clone_url: str | None = None
	) -> str:
		return str(
			frappe.get_doc(
				{
					"doctype": "Pull Request",
					"title": "Change",
					"repository": self.repository,
					"number": number,
					"state": state,
					"head_branch": branch,
					"head_clone_url": clone_url or f"https://github.com/{self.full_name}.git",
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_a_review_is_submitted_once_and_by_the_user_who_started_it(self):
		started = self.start()
		other = "local-other@example.com"
		if not frappe.db.exists("User", other):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": other,
					"first_name": "Other",
					"send_welcome_email": 0,
					"roles": [{"role": "Guard Member"}],
				}
			).insert(ignore_permissions=True)
		frappe.set_user(other)
		try:
			with self.assertRaises(frappe.ValidationError):
				local.submit(started["review_id"], [])
		finally:
			frappe.set_user("Administrator")
		local.submit(started["review_id"], [])
		with self.assertRaises(frappe.ValidationError):
			local.submit(started["review_id"], [])

	def test_the_review_keeps_no_tree_as_head_and_cuts_the_branch(self):
		started = local.start(self.full_name, self.base, self.working_tree_diff(), "b" * 300)
		name = local.submit(started["review_id"], [])["review"]
		review = db.values("Code Review", name, ("head_sha", "branch")) or {}
		self.assertFalse(review["head_sha"])
		self.assertEqual(review["branch"], "b" * local.MAX_BRANCH)

	def test_the_objects_of_the_change_stay_out_of_the_mirror(self):
		started = self.start()
		objects = local._objects(started["review_id"])
		self.assertTrue(objects.is_dir())
		store = store_for(self.repository)
		self.assertIsNone(store.show(started["tree"], "app/notes.py"))
		local.submit(started["review_id"], [])
		self.assertFalse(objects.exists())

	def test_abandoned_objects_are_removed(self):
		objects = local._objects(self.start()["review_id"])
		local.remove_abandoned()
		self.assertTrue(objects.exists())
		old = time.time() - local.SESSION_TTL - 60
		os.utime(objects, (old, old))
		local.remove_abandoned()
		self.assertFalse(objects.exists())

	def test_an_upload_needs_the_secret_and_works_once(self):
		review_id, secret = local.begin(self.full_name)
		diff = self.working_tree_diff().encode()
		with self.assertRaises(frappe.PermissionError):
			local.receive(review_id, "wrong", diff, self.base)
		with self.assertRaises(frappe.ValidationError):
			local.receive(review_id, secret, diff, "not-a-sha")
		with self.assertRaises(frappe.ValidationError):
			local.receive(review_id, secret, b"", self.base)
		# A failed upload keeps the review waiting, so the agent can send the diff again.
		received = local.receive(review_id, secret, diff, self.base, "feature", self.base)
		self.assertEqual(received["files"], 2)
		with self.assertRaises(frappe.PermissionError):
			local.receive(review_id, secret, diff, self.base)
		context = local.context_for(review_id)
		self.assertIn("update_user_email", context["guide"])
		name = local.submit(review_id, [])["review"]
		review = db.values("Code Review", name, ("branch", "head_sha")) or {}
		self.assertEqual(review["branch"], "feature")
		self.assertEqual(review["head_sha"], self.base)

	def test_the_body_is_read_after_the_secret_is_checked(self):
		review_id, _secret = local.begin(self.full_name)
		with self.assertRaises(frappe.PermissionError):
			local.receive(review_id, "wrong", self.no_model, self.base)

	def test_a_diff_of_a_file_in_another_encoding_applies(self):
		(self.repo.path / "app/latin.py").write_bytes("NAME = 'Jos\xe9'\n".encode("latin-1"))
		self.repo.git("add", "-N", "app/latin.py")
		diff = subprocess.run(
			["git", "diff", "--binary", self.base], cwd=self.repo.path, capture_output=True, check=True
		).stdout
		review_id, secret = local.begin(self.full_name)
		local.receive(review_id, secret, diff, self.base)
		tree = local.context_for(review_id)["tree"]
		store = store_for(self.repository).scratch(local._objects(review_id))
		self.assertEqual(store._git("cat-file", "-s", f"{tree}:app/latin.py").strip(), "14")

	def test_a_review_without_its_diff_has_no_context(self):
		review_id, _secret = local.begin(self.full_name)
		with self.assertRaises(frappe.ValidationError):
			local.context_for(review_id)
		with self.assertRaises(frappe.ValidationError):
			local.submit(review_id, [])

	def test_a_diff_that_does_not_apply_fails(self):
		diff = "diff --git a/app/models.py b/app/models.py\n--- a/app/models.py\n+++ b/app/models.py\n@@ -1,1 +1,1 @@\n-nothing here\n+x\n"
		with self.assertRaises(frappe.ValidationError):
			self.start(diff)

	def test_an_unknown_repository_fails(self):
		with self.assertRaises(frappe.PermissionError):
			local.start("acme/nothing-here", self.base, self.working_tree_diff())

	def test_symbol_gives_callers_on_the_default_branch(self):
		definitions = local.symbol(self.full_name, "update_user_email")["definitions"]
		self.assertEqual(definitions[0]["path"], "app/models.py")
		self.assertIn("app/service.py", [c["path"] for c in definitions[0]["callers"]])
