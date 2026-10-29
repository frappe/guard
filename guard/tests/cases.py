from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from guard.llm import breaker
from guard.llm.client import use_responder
from guard.providers.base import PullRequestInfo
from guard.review import pull_requests, trigger
from guard.tests.fakes import FakeAdapter, scripted
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db

MODELS = """def get_user(user_id):
    return {"id": user_id}


def update_user_email(user, email):
    user["email"] = email
    return user
"""

SERVICE = """from app.models import get_user, update_user_email


def change_email(user_id, email):
    user = get_user(user_id)
    return update_user_email(user, email)
"""

AUTO_APPROVE = '[auto_approve]\nenabled = true\nrisk_ceiling = "low"\n'

PATCHED = (
	"guard.review.pipeline",
	"guard.review.state",
	"guard.review.trigger",
	"guard.learning.approve",
	"guard.conversation.threads",
	"guard.conversation.acks",
	"guard.learning.addressed",
)


class PullRequestCase(IntegrationTestCase):
	"""A repository with an open pull request #7 that changes a signature, and a fake provider."""

	def setUp(self):
		frappe.db.set_single_value("Guard Settings", "learn_from_activity", 1)
		# The breaker state lives in the cache and outlives a test.
		breaker.reset()
		self.repo = TempRepo({"app/__init__.py": "", "app/models.py": MODELS, "app/service.py": SERVICE})
		self.base = self.repo.head()
		self.repository = make_repository(self.repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		self.repo.branch_off("feature")
		self.head = self.repo.commit(
			{
				"app/models.py": MODELS.replace(
					"def update_user_email(user, email):", "def update_user_email(user, email, notify):"
				)
			},
			"add notify",
		)
		self.info = PullRequestInfo(
			7,
			"Add notify flag",
			"Adds a flag.",
			"ada",
			"open",
			False,
			"main",
			"feature",
			self.base,
			self.head,
			"https://github.test/pr/7",
		)
		self.adapter = FakeAdapter(self.info)
		self.stack = ExitStack()
		for module in PATCHED:
			self.stack.enter_context(patch(f"{module}.adapter_for_repository", return_value=self.adapter))
		self.pr = str(pull_requests.upsert(self.repository, self.info).name)

	def tearDown(self):
		self.stack.close()
		self.repo.cleanup()

	def row(self, name):
		self.assertTrue(name)
		return (
			db.values(
				"Code Review",
				str(name),
				("status", "error", "posted_count", "confidence_score", "credits", "review_number"),
			)
			or {}
		)

	def review(self, steps):
		with use_responder(scripted(steps)):
			return trigger.consider(self.pr, "open")
