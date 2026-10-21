from contextlib import ExitStack
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from guard.indexing.jobs import ensure_fresh
from guard.llm.client import use_responder
from guard.providers.base import PullRequestInfo
from guard.review import pull_requests, trigger
from guard.tests.fakes import FakeAdapter, scripted
from guard.tests.utils import TempRepo, make_repository

PILOT_API = """import frappe


@frappe.whitelist()
def heartbeat():
    return "ok"


@frappe.whitelist()
def status():
    return "up"
"""

ROUTER = """from atlas.base import Router

api = Router(prefix="api")
"""

ROUTES = """from atlas.router import api

vms = api.subrouter(
    "virtual-machines",
)


@vms.post("<vm_id>/actions/stop")
def stop(vm_id):
    return vm_id


@vms.get("<vm_id>")
def get(vm_id):
    return vm_id
"""

CLIENT = """from central.api.pilot import status


class Client:
    def heartbeat(self):
        return self.forward("central.api.pilot.heartbeat", "GET")

    def stop(self, name):
        return self._request(
            "POST", f"virtual-machines/{quote(name, safe='')}/actions/stop"
        )

    def ping(self):
        return status()
"""

PATCHED = (
	"guard.review.pipeline",
	"guard.review.trigger",
	"guard.learning.approve",
	"guard.conversation.threads",
	"guard.learning.addressed",
)


class TestCrossRepository(IntegrationTestCase):
	"""A provider repository (central + atlas code) and a consumer (pilot) in one team."""

	def setUp(self):
		suffix = frappe.generate_hash(length=6)
		self.provider_repo = TempRepo(
			{
				"central/__init__.py": "",
				"central/api/__init__.py": "",
				"central/api/pilot.py": PILOT_API,
				"atlas/__init__.py": "",
				"atlas/router.py": ROUTER,
				"atlas/routes.py": ROUTES,
			}
		)
		self.consumer_repo = TempRepo({"pilot/__init__.py": "", "pilot/client.py": CLIENT})
		self.provider = make_repository(self.provider_repo, full_name=f"acme/central-{suffix}")
		self.consumer = make_repository(self.consumer_repo, full_name=f"acme/pilot-{suffix}")
		team = frappe.get_doc(
			{
				"doctype": "Guard Team",
				"team_name": f"acme-{suffix}",
				"code_provider": "Test Provider",
				"namespace": f"acme-{suffix}",
			}
		).insert(ignore_permissions=True)
		for repository in (self.provider, self.consumer):
			frappe.db.set_value("Code Repository", repository, "team", team.name)
			ensure_fresh(repository)
		self.stack = ExitStack()

	def tearDown(self):
		self.stack.close()
		self.provider_repo.cleanup()
		self.consumer_repo.cleanup()

	def open_pull_request(self, repo: TempRepo, repository: str, files: dict, number: int) -> FakeAdapter:
		base = repo.head()
		repo.branch_off("feature")
		head = repo.commit(files, "change")
		info = PullRequestInfo(
			number, "Change", "", "ada", "open", False, "main", "feature", base, head, "https://x"
		)
		adapter = FakeAdapter(info)
		for module in PATCHED:
			self.stack.enter_context(patch(f"{module}.adapter_for_repository", return_value=adapter))
		pr = str(pull_requests.upsert(repository, info).name)
		with use_responder(scripted([[("submit_review", {"summary": "s", "confidence_score": 4})]])):
			trigger.consider(pr, "open")
		return adapter

	def test_deleting_what_another_repository_uses(self):
		api = PILOT_API.replace('@frappe.whitelist()\ndef heartbeat():\n    return "ok"\n\n\n', "")
		routes = ROUTES.replace(
			'@vms.post("<vm_id>/actions/stop")\ndef stop(vm_id):\n    return vm_id\n\n\n', ""
		)
		adapter = self.open_pull_request(
			self.provider_repo, self.provider, {"central/api/pilot.py": api, "atlas/routes.py": routes}, 1
		)
		summary = adapter.named("post_comment")[0]
		consumer = frappe.db.get_value("Code Repository", self.consumer, "full_name")
		self.assertIn(f"2 uses in `{consumer}` may break.", summary)
		self.assertIn("<summary><b>Impact</b></summary>", summary)
		self.assertIn("-.->|breaks|", summary)
		titles = [comment.body for comment in adapter.named("create_review")[0]]
		self.assertTrue(any("Removing `heartbeat` breaks 1 use" in t for t in titles), titles)

	def test_calling_what_does_not_exist(self):
		client = CLIENT.replace("central.api.pilot.heartbeat", "central.api.pilot.nope").replace(
			"actions/stop", "actions/hibernate"
		)
		adapter = self.open_pull_request(self.consumer_repo, self.consumer, {"pilot/client.py": client}, 2)
		titles = [comment.body for comment in adapter.named("create_review")[0]]
		self.assertTrue(any("`central.api.pilot.nope` does not exist" in t for t in titles), titles)
		self.assertTrue(any("matches POST virtual-machines/*/actions/hibernate" in t for t in titles), titles)
