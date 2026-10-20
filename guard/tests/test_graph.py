import json

import frappe
from frappe.tests import IntegrationTestCase

from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.graph.overlay import PullRequestGraph
from guard.graph.queries import CodeGraph
from guard.indexing.indexer import Indexer
from guard.indexing.jobs import index_for
from guard.tests.utils import TempRepo, make_repository
from guard.utils import db

MODELS = """class Base:
    def save(self):
        pass


class User(Base):
    def validate_email(self, email):
        return "@" in email


def get_user(user_id):
    return User()


def update_user_email(user, email):
    user.validate_email(email)
    user.save()


def update_user_profile(user, data):
    user.save()
"""

SERVICE = """from app.models import get_user, update_user_email


def change_email(user_id, email):
    user = get_user(user_id)
    update_user_email(user, email)
"""

HOOKS = """doc_events = {
    "Note": {"on_update": "app.service.change_email"},
}
"""

CLIENT = """frappe.call({ method: "app.service.change_email", args: {} });
"""

NOTE = json.dumps(
	{
		"doctype": "DocType",
		"name": "Note",
		"module": "App",
		"fields": [
			{"fieldname": "title", "fieldtype": "Data"},
			{"fieldname": "owner_user", "fieldtype": "Link", "options": "User"},
		],
	},
	indent=1,
)

TEST_SERVICE = """from app.service import change_email


def test_change():
    change_email(1, "a@b")
"""

FILES = {
	"app/__init__.py": "",
	"app/models.py": MODELS,
	"app/service.py": SERVICE,
	"app/hooks.py": HOOKS,
	"app/modules.txt": "App\n",
	"app/doctype/note/note.json": NOTE,
	"app/doctype/note/note.py": "class Note:\n    pass\n",
	"app/tests/test_service.py": TEST_SERVICE,
	"web/client.js": CLIENT,
}


class TestGraph(IntegrationTestCase):
	def setUp(self):
		self.repo = TempRepo(FILES)
		self.repository = make_repository(self.repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		self.index = index_for(self.repository)
		Indexer(self.index).run()
		self.graph = CodeGraph(self.index)

	def tearDown(self):
		self.repo.cleanup()

	def one(self, name, kind=None):
		matches = self.graph.definitions(name, kind)
		self.assertTrue(matches, f"no definition for {name}")
		return matches[0]

	def test_index_counts(self):
		row = db.values("Repository Index", self.index, ("status", "sha", "entity_count")) or {}
		self.assertEqual(row["status"], "completed")
		self.assertEqual(row["sha"], self.repo.head())
		self.assertGreater(row["entity_count"], 10)

	def test_callers_resolve_through_imports(self):
		callers = self.graph.callers(self.one("update_user_email"))
		self.assertIn(("app/service.py", True), [(c.path, c.resolved) for c in callers])

	def test_self_method_and_inheritance(self):
		callees = {c.name: c for c in self.graph.callees(self.one("update_user_email"))}
		self.assertTrue(callees["validate_email"].resolved)
		save = callees["save"].entity
		self.assertEqual(save.qualified_name if save else None, "app.models.Base.save")

	def test_frappe_edges(self):
		change = self.one("change_email")
		paths = {(c.path, c.resolved) for c in self.graph.callers(change)}
		self.assertIn(("web/client.js", True), paths)
		self.assertIn(("app/hooks.py", True), paths)
		note = self.one("doctype:Note")
		self.assertEqual(note.kind, "doctype")
		references = {r.path for r in self.graph.references(note)}
		self.assertIn("app/doctype/note/note.py", references)

	def test_siblings_and_search(self):
		siblings = [s.entity_name for s in self.graph.siblings(self.one("update_user_email"))]
		self.assertIn("update_user_profile", siblings)
		hits = self.graph.search("update user email")
		self.assertEqual(hits[0].path, "app/models.py")

	def test_incremental_keeps_names_and_repairs_edges(self):
		before = self.one("update_user_email").name
		self.repo.commit(
			{"app/models.py": "\n\n" + MODELS + "\n\ndef new_helper():\n    return get_user(1)\n"}, "shift"
		)
		Indexer(self.index).run()
		self.assertEqual(self.one("update_user_email").name, before)
		callers = [c.path for c in self.graph.callers(self.one("get_user"))]
		self.assertIn("app/service.py", callers)
		self.assertIn("app/models.py", callers)

	def test_overlay_contract_change(self):
		base = self.repo.head()
		self.repo.branch_off("feature")
		head = self.repo.commit(
			{
				"app/models.py": MODELS.replace(
					"def update_user_email(user, email):", "def update_user_email(user, email, notify):"
				)
			},
			"change signature",
		)
		store = store_for(self.repository)
		store.fetch_sha(head, str(self.repo.path))
		files = parse_diff(store.diff(base, head))
		overlay = PullRequestGraph(self.graph, store, base, head, files, frappe_app=True)
		changed = {c.qualified_name: c for c in overlay.build()}
		entity = changed["app.models.update_user_email"]
		self.assertTrue(entity.contract_changed)
		self.assertEqual([link.path for link in entity.not_updated], ["app/service.py"])
		self.assertIn("update_user_email(user, email)", entity.at_risk[0].snippet)
		radius = overlay.blast_radius(list(changed.values()))
		self.assertEqual(radius.not_updated, 1)
		self.assertIn("app/tests/test_service.py", radius.tests)
