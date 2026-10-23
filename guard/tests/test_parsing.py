from frappe.tests import UnitTestCase

from guard.git.diff import parse_diff
from guard.indexing import languages
from guard.indexing.chunker import chunk_file
from guard.indexing.parser import parse_file

DIFF = """diff --git a/app/a.py b/app/a.py
index 111..222 100644
--- a/app/a.py
+++ b/app/a.py
@@ -1,4 +1,5 @@ def top():
 def top():
-    return 1
+    value = compute()
+    return value
{blank}
 x = 1
diff --git a/old.sql b/new.sql
similarity index 90%
rename from old.sql
rename to new.sql
--- a/old.sql
+++ b/new.sql
@@ -1,2 +1,2 @@
--- comment line
+-- new comment
 select 1;
diff --git a/gone.txt b/gone.txt
deleted file mode 100644
--- a/gone.txt
+++ /dev/null
@@ -1 +0,0 @@
-bye
""".format(blank=" ")


class TestDiff(UnitTestCase):
	def test_files_and_status(self):
		files = parse_diff(DIFF)
		self.assertEqual(
			[(f.path, f.status) for f in files],
			[("app/a.py", "modified"), ("new.sql", "renamed"), ("gone.txt", "deleted")],
		)
		self.assertEqual(files[1].old_path, "old.sql")

	def test_line_numbers(self):
		diff = parse_diff(DIFF)[0]
		self.assertEqual(diff.added_lines(), {2, 3})
		self.assertEqual(diff.commentable_lines(), {1, 2, 3, 4, 5})
		self.assertEqual((diff.additions, diff.deletions), (2, 1))
		self.assertTrue(diff.touches(3, 3))
		self.assertFalse(diff.touches(5, 5))

	def test_sql_comment_deletion_is_content(self):
		diff = parse_diff(DIFF)[1]
		self.assertEqual(diff.deletions, 1)
		self.assertEqual(diff.hunks[0].lines[0].text, "-- comment line")

	def test_annotated(self):
		text = parse_diff(DIFF)[0].annotated()
		self.assertIn("    2 +     value = compute()", text)
		self.assertIn("      -     return 1", text)


class TestLanguages(UnitTestCase):
	def test_bases(self):
		self.assertEqual(languages.bases("class A(Base, mod.Mixin):"), ["Base", "Mixin"])
		self.assertEqual(languages.bases("public class S extends B implements I, J"), ["B", "I", "J"])
		self.assertEqual(languages.bases("class S < B"), ["B"])
		self.assertEqual(languages.bases("class A(metaclass=Meta):"), [])

	def test_python_imports(self):
		refs = languages.imports("python", "from a.b import c as d, e\nimport x.y as z\nfrom . import sib")
		self.assertEqual(refs[0].module, "a.b")
		self.assertEqual(refs[0].names, {"d": "c", "e": "e"})
		self.assertEqual(refs[1].alias, "z")
		self.assertTrue(refs[2].relative)

	def test_js_imports(self):
		refs = languages.imports(
			"typescript", 'import Def, { a, b as c } from "./m";\nconst r = require("../r");'
		)
		self.assertEqual(refs[0].names, {"a": "a", "c": "b", "Def": "default"})
		self.assertEqual(refs[1].module, "../r")


class TestParser(UnitTestCase):
	SOURCE = """import frappe
from app.models import save as persist


class Service(Base):
    def run(self, x):
        self.check(x)
        return persist(x)

    def check(self, x):
        frappe.enqueue("app.jobs.process", x=x)


@frappe.whitelist(allow_guest=True)
def entry():
    Service().run(1)
"""

	def test_entities(self):
		parsed = parse_file("app/service.py", self.SOURCE)
		kinds = [(e.kind, e.qualified_name) for e in parsed.entities]
		self.assertIn(("class", "app.service.Service"), kinds)
		self.assertIn(("method", "app.service.Service.run"), kinds)
		self.assertIn(("function", "app.service.entry"), kinds)
		entry = next(e for e in parsed.entities if e.name == "entry")
		self.assertEqual(entry.decorators, ["@frappe.whitelist(allow_guest=True)"])

	def test_edges(self):
		parsed = parse_file("app/service.py", self.SOURCE)
		calls = {(e.target_name, e.target_text) for e in parsed.edges if e.kind == "calls"}
		self.assertIn(("check", "self.check"), calls)
		self.assertIn(("persist", "persist"), calls)
		self.assertIn(("run", "Service().run"), calls)
		refs = [e.target_name for e in parsed.edges if e.kind == "references"]
		self.assertEqual(refs, ["app.jobs.process"])
		inherits = [e.target_name for e in parsed.edges if e.kind == "inherits"]
		self.assertEqual(inherits, ["Base"])

	def test_one_entity_for_two_tags(self):
		# Two tag patterns match `a.B = function B() {}`, at the same line with the same name.
		parsed = parse_file("app/sms.js", "app.SMS = function SMS(doc) {\n\tthis.doc = doc;\n};\n")
		keys = [e.key for e in parsed.entities]
		self.assertEqual(len(keys), len(set(keys)))
		self.assertEqual([e.name for e in parsed.entities if e.kind != "file"], ["SMS"])

	def test_unknown_language_is_file_only(self):
		parsed = parse_file("notes/readme.txt", "hello")
		self.assertEqual(len(parsed.entities), 1)

	def test_chunks(self):
		body = "\n".join(f"    x{i} = {i}" for i in range(400))
		source = f"def big():\n{body}\n\ndef small():\n    return 1\n"
		chunks = chunk_file(parse_file("m.py", source), source)
		big = [c for c in chunks if "m.big" in c.content.splitlines()[0]]
		self.assertGreater(len(big), 3)
		self.assertTrue(all(c.end_line - c.start_line < 100 for c in big))
		self.assertTrue(any("m.small" in c.content for c in chunks))


class TestAnchor(UnitTestCase):
	def test_deleted_lines_anchor_left(self):
		from guard.review.findings import Finding, FindingFilter

		diff = parse_diff(DIFF)
		finding = Finding("gone.txt", 1, 1, "P1", "logic", "Removed file is still used", "x")
		FindingFilter(diff, {"strictness": 2}).anchor(finding)
		self.assertEqual((finding.side, finding.outside_diff), ("LEFT", False))
		head = Finding("app/a.py", 3, 3, "P1", "logic", "t", "x")
		FindingFilter(diff, {"strictness": 2}).anchor(head)
		self.assertEqual((head.side, head.start_line), ("RIGHT", 3))


class TestChecks(UnitTestCase):
	def test_secret_and_frappe_checks(self):
		from guard.review import checks

		text = (
			'diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n@@ -1 +1,4 @@\n x = 1\n+TOKEN = "ghp_'
			+ "a" * 36
			+ '"\n+rows = frappe.db.sql(f"select * from tabUser where name = {name}")\n+password = "changeme-example"\n'
		)
		files = parse_diff(text)
		secrets = checks.secret_findings(files)
		self.assertEqual([(f.severity, f.start_line) for f in secrets], [("P0", 2)])
		frappe_pack = checks.frappe_findings(files)
		self.assertEqual([f.title for f in frappe_pack], ["SQL built from strings"])
		self.assertNotIn("ghp_", checks.mask_secrets(files[0].annotated()))


class TestWebhookHeaders(UnitTestCase):
	def test_signature_and_event_with_any_header_case(self):
		import hashlib
		import hmac

		from guard.webhooks import github

		body = b'{"action": "opened"}'
		signature = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
		# What the endpoint builds from Werkzeug headers, whatever case GitHub sends.
		headers = {
			k.lower(): v
			for k, v in {
				"X-Github-Event": "pull_request",
				"X-Hub-Signature-256": signature,
				"X-GitHub-Delivery": "d1",
			}.items()
		}
		self.assertTrue(github.verify(headers, body, "s3cret"))
		self.assertFalse(github.verify(headers, body, "wrong"))
		self.assertEqual((github.event_name(headers), github.delivery_id(headers)), ("pull_request", "d1"))

	def test_wanted_drops_ci_noise(self):
		from guard.webhooks import github

		repo = {"full_name": "acme/app", "owner": {"login": "acme"}}
		run = {"pull_requests": [{"number": 7}]}
		self.assertFalse(
			github.wanted("check_run", {"action": "completed", "check_run": run, "repository": repo})
		)
		self.assertFalse(github.wanted("check_suite", {"action": "completed", "repository": repo}))
		self.assertTrue(
			github.wanted("check_run", {"action": "rerequested", "check_run": run, "repository": repo})
		)
		# A payload the parser cannot read goes on to the handler, which stores the error.
		self.assertTrue(
			github.wanted("check_run", {"action": "rerequested", "check_run": {"pull_requests": [{}]}})
		)


class TestCallFit(UnitTestCase):
	def fit(self, signature, call, name="place_order"):
		from guard.graph import calls

		params = calls.parse_params(signature)
		parsed = calls.parse_call([call], 1, name)
		return calls.fits(params, parsed) if params and parsed else None

	def test_default_keeps_old_calls(self):
		self.assertTrue(self.fit("def place_order(customer, items, code=None):", "place_order(c, items)"))

	def test_new_required_argument_breaks(self):
		self.assertFalse(self.fit("def place_order(customer, items, code):", "x = place_order(c, items)"))

	def test_keywords_and_methods(self):
		self.assertTrue(self.fit("def assign(self, agent, notify):", "t.assign(a, notify=1)", "assign"))
		self.assertFalse(self.fit("def assign(self, agent):", "t.assign(a, notify=1)", "assign"))
		self.assertFalse(self.fit("def assign(self, agent, *, notify):", "t.assign(a)", "assign"))

	def test_too_many_arguments_break(self):
		self.assertFalse(self.fit("def place_order(customer):", "place_order(c, items)"))

	def test_unknown_cases(self):
		self.assertIsNone(self.fit("def place_order(customer, items):", "place_order(*args)"))
		self.assertIsNone(self.fit("function placeOrder(a, b)", "placeOrder(a)", "placeOrder"))

	def test_multiline_call(self):
		from guard.graph import calls

		lines = ["place_order(", "    c,", "    [x for x in (1, 2)],", ")"]
		call = calls.parse_call(lines, 1, "place_order")
		self.assertEqual(call.positional if call else None, 2)


class TestRoutes(UnitTestCase):
	def test_router_chain_and_definitions(self):
		from guard.indexing import routes

		router_file = [
			'API = "atlas"',
			"root = Router(",
			"    prefix=API,",
			")",
			"vms = root.subrouter(",
			'    "virtual-machines",',
			")",
		]
		table = {r.name: (r.parent, r.segment) for r in routes.routers(router_file)}
		[route] = routes.definitions(['@vms.post("<vm_id>/actions/stop")'])
		self.assertEqual((route.verb, route.router), ("POST", "vms"))
		self.assertEqual(routes.full_path(route, table), "atlas/virtual-machines/*/actions/stop")

	def test_client_calls(self):
		from guard.indexing import routes

		lines = [
			"path = f\"hosts/{quote(host_id, safe='')}/access/grant\"",
			'self._request("POST", path)',
			"self._request(",
			'    "PATCH", f"virtual-machines/{name}/disk"',
			")",
			'value = payload.get("metadata")',
			'requests.get("/api/v1/images/download")',
		]
		calls = [(c.verb, c.normalized, c.line) for c in routes.client_calls(lines, set())]
		self.assertEqual(
			calls,
			[
				("POST", "hosts/*/access/grant", 2),
				("PATCH", "virtual-machines/*/disk", 3),
				("GET", "images/download", 7),
			],
		)

	def test_strength(self):
		from guard.indexing import routes

		route = "atlas/virtual-machines/*/actions/stop"
		self.assertEqual(routes.strength(route, "virtual-machines/*/actions/stop"), 3)
		self.assertEqual(routes.strength(route, "virtual-machines/*/actions/hibernate"), 0)
		self.assertEqual(routes.strength("atlas/vms/*/public-ipv4", "vms/*/public-ipv*"), 2)
		self.assertEqual(routes.strength("atlas/images", "virtual-machines"), 0)


class TestImpactDiagram(UnitTestCase):
	def test_counts_tests_instead_of_drawing_them(self):
		from guard.graph.overlay import ChangedEntity, impact_diagram
		from guard.graph.queries import Entity, Link

		def link(path, line, name):
			caller = Entity(name, "function", name, f"app.{name}", path, None, line, line, "", "python")
			return Link(caller, "build", "build()", path, line, True)

		base = Entity(
			"b", "function", "build", "app.api.build", "app/api.py", None, 1, 3, "def build(a)", "python"
		)
		entity = ChangedEntity(
			"app/api.py",
			"function",
			"build",
			"app.api.build",
			"modified",
			base=base,
			head_signature="def build(a, b)",
		)
		entity.callers = [
			link("app/views.py", 10, "get_settings"),
			*(link("app/tests/test_api.py", n, f"test_case_{n}") for n in (1, 2, 3)),
			link("app/tests/test_api.py", 9, "test_old_call"),
		]
		entity.not_updated = [entity.callers[-1]]
		diagram = impact_diagram([entity])
		self.assertIn('["get_settings"]', diagram)
		self.assertIn('["3 tests of build"]:::tests', diagram)
		self.assertNotIn("test_case_1", diagram)
		# A test that the new signature breaks is still drawn.
		self.assertIn('["test_old_call"]', diagram)
		self.assertIn("-.->|may break|", diagram)
