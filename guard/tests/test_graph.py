import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from guard.git import maintenance
from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.git.store import GitError, GitStore
from guard.graph.overlay import PullRequestGraph
from guard.graph.queries import CodeGraph
from guard.indexing import indexer
from guard.indexing.indexer import IndexBusy, Indexer, index_lock
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

	def parsed_paths(self) -> list[str]:
		"""Run the indexer and return the paths it parsed."""
		parsed = []

		def parse(path, source):
			parsed.append(path)
			return real(path, source)

		real = indexer.parse_file
		with patch("guard.indexing.indexer.parse_file", side_effect=parse):
			Indexer(self.index).run()
		return parsed

	def test_push_parses_only_changed_files(self):
		self.repo.commit({"app/models.py": MODELS + "\n\ndef extra():\n    pass\n"}, "extra")
		# What a push does before the job runs.
		frappe.db.set_value("Repository Index", self.index, "status", "submitted")
		self.assertEqual(self.parsed_paths(), ["app/models.py"])
		self.assertTrue(self.graph.definitions("extra"))

	def test_run_after_a_dead_run_parses_everything(self):
		# A worker restart leaves the status of the run it killed.
		frappe.db.set_value("Repository Index", self.index, "status", "processing")
		selected = db.value("Repository Index", self.index, "num_files")
		self.assertEqual(len(self.parsed_paths()), selected)
		frappe.db.set_value("Repository Index", self.index, "status", "failed")
		frappe.db.set_value("Repository Index", self.index, "sha", None)
		self.assertEqual(len(self.parsed_paths()), selected)

	def test_one_run_at_a_time(self):
		with index_lock(self.index) as acquired:
			self.assertTrue(acquired)
			with self.assertRaises(IndexBusy):
				Indexer(self.index).run()
		self.assertEqual(db.value("Repository Index", self.index, "status"), "completed")
		self.assertEqual(self.parsed_paths(), [])

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
		overlay = PullRequestGraph(
			self.graph,
			lambda path: store.show(head, path),
			lambda path: store.show(base, path),
			files,
			frappe_app=True,
		)
		changed = {c.qualified_name: c for c in overlay.build()}
		entity = changed["app.models.update_user_email"]
		self.assertTrue(entity.contract_changed)
		self.assertEqual([link.path for link in entity.not_updated], ["app/service.py"])
		self.assertIn("update_user_email(user, email)", entity.at_risk[0].snippet)
		radius = overlay.blast_radius(list(changed.values()))
		self.assertEqual(radius.not_updated, 1)
		self.assertIn("app/tests/test_service.py", radius.tests)


class TestGitStore(IntegrationTestCase):
	def test_fetch_branches_skips_other_branches(self):
		repo = TempRepo({"a.py": "x = 1\n"})
		self.addCleanup(repo.cleanup)
		repo.branch_off("hotfix")
		repo.commit({"b.py": "y = 2\n"}, "hotfix")
		store = store_for(make_repository(repo, full_name=f"acme/{frappe.generate_hash(length=6)}"))
		store.fetch_branches("main")
		self.assertTrue(store.rev_parse("refs/heads/main"))
		with self.assertRaises(GitError):
			store.rev_parse("refs/heads/hotfix")
		store.fetch_branches()
		self.assertEqual(store.rev_parse("refs/heads/hotfix"), repo.head())

	def test_maintenance_drops_refs_of_closed_pull_requests(self):
		repo = TempRepo({"a.py": "x = 1\n"})
		self.addCleanup(repo.cleanup)
		repository = make_repository(repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		git = store_for(repository)
		for number in (1, 2):
			git.fetch_branch("main", number)
		frappe.get_doc(
			{
				"doctype": "Pull Request",
				"title": "Open",
				"repository": repository,
				"number": 1,
				"state": "open",
			}
		).insert(ignore_permissions=True)
		maintenance.maintain(git)
		self.assertEqual(git.refs("refs/guard/pr/"), ["refs/guard/pr/1"])
		self.assertGreater(git.size_kb(), 0)

	def test_fetch_waits_for_the_lock(self):
		repo = TempRepo({"a.py": "x = 1\n"})
		self.addCleanup(repo.cleanup)
		git = store_for(make_repository(repo, full_name=f"acme/{frappe.generate_hash(length=6)}"))
		git.fetch_branches("main")
		with git._lock(), patch("guard.git.store.TIMEOUT", 0), patch("guard.git.store.LOCK_POLL", 0):
			with self.assertRaises(GitError):
				git.fetch_branches("main")


class TestSameNameInTwoFiles(IntegrationTestCase):
	def test_routes_and_doctypes_with_one_name(self):
		# Three services can each serve GET /health; two apps can each have a Settings DocType.
		doctype = json.dumps({"doctype": "DocType", "name": "Settings", "module": "Core", "fields": []})
		repo = TempRepo(
			{
				"svc_a/app.js": 'app.get("/health", (req, res) => res.send("ok"));\n',
				"svc_b/server.py": '@app.get("/health")\ndef health():\n    return "ok"\n',
				"app/hooks.py": 'app_name = "app"\n',
				"app/modules.txt": "One\nTwo\n",
				"app/one/doctype/settings/settings.json": doctype,
				"app/two/doctype/settings/settings.json": doctype,
			}
		)
		self.addCleanup(repo.cleanup)
		index = index_for(make_repository(repo, full_name=f"acme/{frappe.generate_hash(length=6)}"))
		Indexer(index).run()
		self.assertEqual(db.value("Repository Index", index, "status"), "completed")
		graph = CodeGraph(index)
		self.assertEqual(
			{e.path for e in graph.definitions("route:GET app:health", "route")},
			{"svc_a/app.js", "svc_b/server.py"},
		)
		self.assertEqual(len(graph.definitions("doctype:Settings", "doctype")), 2)

	def test_a_name_clash_does_not_fail_the_index(self):
		# `x.js` and `x.ts` share the module path `app/x`, so both define `app/x.foo`.
		source = "function foo() {\n  return 1;\n}\n"
		repo = TempRepo({"app/x.js": source, "app/x.ts": source})
		self.addCleanup(repo.cleanup)
		index = index_for(make_repository(repo, full_name=f"acme/{frappe.generate_hash(length=6)}"))
		Indexer(index).run()
		self.assertEqual(db.value("Repository Index", index, "status"), "completed")
		paths = {e.path for e in CodeGraph(index).definitions("app/x.foo", "function")}
		self.assertEqual(paths, {"app/x.js", "app/x.ts"})


class TestIndexCheck(IntegrationTestCase):
	def test_reports_and_raises(self):
		from guard.indexing.check import IndexCheckFailed, check

		repo = TempRepo(FILES)
		self.addCleanup(repo.cleanup)
		name = f"acme/{frappe.generate_hash(length=6)}"
		[result] = check([(name, str(repo.path), "main")])
		self.assertFalse(result.error)
		self.assertGreater(result.entities, 10)
		with patch.object(Indexer, "_run", side_effect=RuntimeError("parser broke")):
			with self.assertRaises(IndexCheckFailed) as raised:
				check([(name, str(repo.path), "main")])
		self.assertIn("parser broke", str(raised.exception))


class TestScopedKnowledgeBase(IntegrationTestCase):
	def test_large_repository_covers_the_busiest_areas(self):
		from guard.knowledge import builder

		files = {
			f"{area}/m{n}.py": f"def f{n}():\n    return {n}\n"
			for area in ("billing", "stock")
			for n in range(4)
		}
		repo = TempRepo(files)
		self.addCleanup(repo.cleanup)
		repository = make_repository(repo, full_name=f"acme/{frappe.generate_hash(length=6)}")
		index = index_for(repository)
		Indexer(index).run()
		frappe.db.set_value("Code Repository", repository, "default_index", index)
		frappe.get_doc(
			{
				"doctype": "Code Review",
				"repository": repository,
				"source": "cli",
				"status": "Completed",
				"trigger": "cli",
				"changed_files": json.dumps([{"path": "stock/m1.py", "status": "modified"}]),
			}
		).insert(ignore_permissions=True)
		with patch.object(builder, "LARGE_REPO_FILES", 4), patch.object(builder, "FOCUS_AREAS", 1):
			builder.KnowledgeBuilder(repository).build()
		paths = set(frappe.get_all("Knowledge Base Document", {"repository": repository}, pluck="path"))
		self.assertIn("docs/stock.md", paths)
		self.assertNotIn("docs/billing.md", paths)
		overview = db.value(
			"Knowledge Base Document", {"repository": repository, "path": "index.md"}, "content"
		)
		self.assertIn("covers only the 1 areas", str(overview))
