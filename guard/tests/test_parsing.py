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
