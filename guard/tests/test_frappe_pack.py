import json
from textwrap import dedent

from frappe.tests import UnitTestCase

from guard.config.resolver import ConfigResolver
from guard.git.diff import parse_diff
from guard.review import frappe_pack
from guard.review.frappe_pack.engine import parse

# (rule id, path, code that breaks the rule, code that keeps it). The first line of each pair that
# differs is where the finding must be.
CASES = [
	(
		"frappe-sql-format-injection",
		"app/api.py",
		'def f(name):\n\tfrappe.db.sql(f"select * from tabUser where name = {name}")',
		'def f(name):\n\tfrappe.db.sql("select * from tabUser where name = %s", name)',
	),
	(
		"frappe-sql-format-injection",
		"app/api.py",
		'def f(name):\n\tquery = "select * from tabUser where name = %s" % name\n\tfrappe.db.sql(query)',
		'def f(name):\n\tquery = "select * from tabUser where name = %s"\n\tfrappe.db.sql(query, name)',
	),
	(
		"guest-whitelisted-method",
		"app/api.py",
		"@frappe.whitelist(allow_guest=True)\ndef ping():\n\treturn frappe.get_all('Note')",
		"@frappe.whitelist(allow_guest=True)\ndef ping():\n\tfrappe.only_for('System Manager')\n\treturn 1",
	),
	(
		"unchecked-frappe-permission-call",
		"app/api.py",
		"def f(doc):\n\tfrappe.has_permission('Note', 'read', doc)",
		"def f(doc):\n\tfrappe.has_permission('Note', 'read', doc, throw=True)",
	),
	(
		"whitelisted-side-effect-on-get",
		"app/api.py",
		"@frappe.whitelist()\ndef f():\n\tfrappe.sendmail(recipients=['a@b.c'])",
		"@frappe.whitelist(methods=['POST'])\ndef f():\n\tfrappe.sendmail(recipients=['a@b.c'])",
	),
	(
		"frappe-format-string-injection",
		"app/api.py",
		"try:\n\tpass\nexcept Exception as e:\n\tfrappe.throw(_('Failed: {0}').format(e))",
		"try:\n\tpass\nexcept Exception as e:\n\tfrappe.throw(_('Failed: {0}').format(str(e)))",
	),
	(
		"frappe-test-whitelist-missing-protection",
		"app/tests/test_api.py",
		"@frappe.whitelist()\ndef helper():\n\tpass",
		"@whitelist_for_tests()\n@frappe.whitelist()\ndef helper():\n\tpass",
	),
	(
		"frappe-sql-format-injection",
		"app/api.py",
		'def f(name):\n\tfrappe.db.sql("select * from tabUser where name = %s" % name)',
		# A literal formatted with a literal is not built from input.
		'def f():\n\tfrappe.db.sql("select * from %s" % "tabUser")',
	),
	(
		"frappe-manual-commit",
		"app/api.py",
		"@frappe.whitelist(methods=['POST'])\ndef f():\n\tfrappe.db.commit()",
		"def job():\n\tfrappe.db.commit()",
	),
	(
		"frappe-breaks-multitenancy",
		"app/utils.py",
		"COMPANY = frappe.db.get_single_value('Global Defaults', 'default_company')",
		"def company():\n\treturn frappe.db.get_single_value('Global Defaults', 'default_company')",
	),
	(
		"frappe-cache-breaks-multitenancy",
		"app/utils.py",
		"def f():\n\treturn frappe.cache().get('key')",
		"def f():\n\treturn frappe.cache().get_value('key')",
	),
	(
		"frappe-modifying-but-not-comitting",
		"app/doctype/note/note.py",
		"class Note(Document):\n\tdef on_submit(self):\n\t\tself.title = 'Done'",
		"class Note(Document):\n\tdef on_submit(self):\n\t\tself.title = 'Done'\n\t\tself.db_set('title', self.title)",
	),
	(
		"frappe-after-save-controller-hook",
		"app/doctype/note/note.py",
		"class Note(Document):\n\tdef after_save(self):\n\t\tpass",
		"class Note(Document):\n\tdef on_update(self):\n\t\tpass",
	),
	(
		"frappe-modifying-child-tables-while-iterating",
		"app/doctype/note/note.py",
		"class Note(Document):\n\tdef validate(self):\n\t\tfor row in self.items:\n\t\t\tself.remove(row)",
		"class Note(Document):\n\tdef validate(self):\n\t\tfor row in list(self.items):\n\t\t\tself.remove(row)",
	),
	(
		"frappe-same-key-assigned-twice",
		"app/utils.py",
		"values = {'a': 1,\n\t'a': 2}",
		"values = {'a': 1,\n\t'a ': 2}",
	),
	(
		"frappe-redis-flush",
		"app/utils.py",
		"def f():\n\tfrappe.cache.flushall()",
		"def f():\n\tfrappe.cache.flushdb()",
	),
	(
		"frappe-overriding-local-proxies",
		"app/utils.py",
		"def f():\n\tfrappe.flags = {}",
		"def f():\n\tfrappe.local.flags = {}",
	),
	(
		"frappe-single-value-type-safety",
		"app/utils.py",
		"def f():\n\treturn frappe.db.get_value('System Settings', None, 'country')",
		"def f():\n\treturn frappe.db.get_single_value('System Settings', 'country')",
	),
	(
		"frappe-set-value-semantics",
		"app/utils.py",
		"def f():\n\tfrappe.db.set_value('System Settings', 'System Settings', 'country', 'India')",
		"def f():\n\tfrappe.db.set_single_value('System Settings', 'country', 'India')",
	),
	(
		"frappe-qb-incorrect-order-usage",
		"app/utils.py",
		"def f(t):\n\treturn q.orderby(t.creation, frappe.qb.desc)",
		"def f(t):\n\treturn q.orderby(t.creation, order=frappe.qb.desc)",
	),
	(
		"frappe-enqueue-without-after-commit",
		"app/doctype/note/note.py",
		"class Note(Document):\n\tdef on_update(self):\n\t\tfrappe.enqueue('app.job')",
		"class Note(Document):\n\tdef on_update(self):\n\t\tfrappe.enqueue('app.job', enqueue_after_commit=True)",
	),
	(
		"frappe-realtime-pick-room",
		"app/utils.py",
		"def f():\n\tfrappe.publish_realtime('done', {})",
		"def f(doc):\n\tfrappe.publish_realtime('done', {}, doctype=doc.doctype, docname=doc.name)",
	),
	(
		"frappe-monkey-patching-not-allowed",
		"app/__init__.py",
		"from frappe.model.document import Document\nDocument.run_method = run_method",
		"from frappe.model.document import Document\nrun = Document.run_method",
	),
	(
		"frappe-query-debug-statement",
		"app/utils.py",
		"def f():\n\treturn frappe.get_all('Note', debug=True)",
		"def f():\n\treturn frappe.get_all('Note')",
	),
	(
		"override-doctype-class",
		"app/hooks.py",
		"override_doctype_class = {'ToDo': 'app.todo.ToDo'}",
		"extend_doctype_class = {'ToDo': 'app.todo.ToDo'}",
	),
	(
		"frappe-translation-empty-string",
		"app/utils.py",
		"label = _('')",
		"label = _('Name')",
	),
	(
		"frappe-translation-variable-only",
		"app/utils.py",
		"def f(a, b):\n\treturn _('{0} {1}').format(a, b)",
		"def f(a, b):\n\treturn _('{0} of {1}').format(a, b)",
	),
	(
		"frappe-translation-trailing-spaces",
		"app/utils.py",
		"label = _('Name ')",
		"label = _('Name') + ' '",
	),
	(
		"frappe-translation-python-formatting",
		"app/utils.py",
		"def f(name):\n\treturn _(f'Hello {name}')",
		"def f(name):\n\treturn _('Hello {0}').format(name)",
	),
	(
		"frappe-translation-python-splitting",
		"app/utils.py",
		"def f():\n\treturn _('Hello') + _('World')",
		"def f():\n\treturn _('Hello World')",
	),
	(
		"frappe-untranslated-label-in-translated-message",
		"app/doctype/note/note.py",
		"class Note(Document):\n\tdef validate(self):\n\t\tfrappe.throw(_('{0} is wrong').format(self.meta.get_label('title')))",
		"class Note(Document):\n\tdef validate(self):\n\t\tfrappe.throw(_('{0} is wrong').format(_(self.meta.get_label('title'))))",
	),
	(
		"frappe-missing-translate-function-python",
		"app/utils.py",
		"def f():\n\tfrappe.throw('Not allowed')",
		"def f():\n\tfrappe.throw(_('Not allowed'))",
	),
	(
		"frappe-missing-translate-function-in-report-python",
		"app/report/sales/sales.py",
		"def columns():\n\treturn [{'fieldname': 'qty', 'label': 'Qty'}]",
		"def columns():\n\treturn [{'fieldname': 'qty', 'label': _('Qty')}]",
	),
	(
		"frappe-missing-translate-function-js",
		"app/public/js/note.js",
		'frappe.msgprint("Saved");',
		'frappe.msgprint(__("Saved"));',
	),
	(
		"frappe-missing-translation-button-text",
		"app/doctype/note/note.js",
		'frm.add_custom_button("Close", () => {});',
		'frm.add_custom_button(__("Close"), () => {});',
	),
	(
		"frappe-translation-empty-string",
		"app/public/js/note.js",
		'let label = __("");',
		'let label = __("Name");',
	),
	(
		"frappe-translation-js-formatting",
		"app/public/js/note.js",
		"let label = __(`Hello ${name}`);",
		'let label = __("Hello {0}", [name]);',
	),
	(
		"frappe-translation-js-splitting",
		"app/public/js/note.js",
		'let label = __("Hello") + __("World");',
		'let label = __("Hello World");',
	),
	(
		"frappe-translated-values-in-business-logic",
		"app/report/sales/sales.js",
		'frappe.query_reports["Sales"] = {filters: [{fieldname: "period",\n\toptions: [__("Monthly"), __("Yearly")]}]};',
		'frappe.query_reports["Sales"] = {filters: [{fieldname: "period",\n\toptions: [{label: __("Monthly"), value: "Monthly"}]}]};',
	),
	(
		"frappe-cur-frm-usage",
		"app/doctype/note/note.js",
		"cur_frm.refresh();",
		"frm.refresh();",
	),
	(
		"frappe-incorrect-debounce",
		"app/public/js/note.js",
		"frappe.utils.debounce(save, 300)();",
		"const debounced = frappe.utils.debounce(save, 300);",
	),
	(
		"relaxed-permissions",
		"app/doctype/note/note.json",
		'{"permissions": [\n{"role": "All", "read": 1}]}',
		'{"permissions": [\n{"role": "Note User", "read": 1}]}',
	),
]
# Rules that only report when a config turns them on.
OFF_CASES = [
	("frappe-security-file-traversal", "app/utils.py", "def f(p):\n\treturn open(p).read()"),
	("frappe-codeinjection-eval", "app/utils.py", "def f(code):\n\treturn eval(code)"),
	("frappe-ssti", "app/utils.py", "def f(t):\n\treturn frappe.render_template(t, {})"),
	("overusing-args", "app/utils.py", "def f(args):\n\treturn args"),
	("frappe-setuser", "app/api.py", "def f(user):\n\tfrappe.set_user(user)"),
	("useless-get-doc-dict", "app/utils.py", "def f():\n\treturn frappe.get_doc(dict(doctype='Note'))"),
	("frappe-print-function-in-doctypes", "app/doctype/note/note.py", "print('hi')"),
	("frappe-no-functional-code", "app/utils.py", "names = list(map(str, rows))"),
	("missing-argument-type-hint", "app/api.py", "@frappe.whitelist()\ndef f(name):\n\tpass"),
	("use-vanilla-js-include", "app/public/js/note.js", 'if (in_list(roles, "Admin")) {}'),
	(
		"frappe-modifying-but-not-comitting-other-method",
		"app/doctype/note/note.py",
		"class Note(Document):\n\tdef on_update(self):\n\t\tself.mark()\n\n\tdef mark(self):\n\t\tself.title = 'x'",
	),
]


def added(path: str, code: str):
	"""A diff that adds the file `path` with `code` in it."""
	lines = code.splitlines()
	header = f"diff --git a/{path} b/{path}\nnew file mode 100644\n--- /dev/null\n+++ b/{path}\n"
	return parse_diff(f"{header}@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines))


def check(path: str, code: str, settings: dict | None = None, read=None):
	files = added(path, code)
	head = read or (lambda p: code if p == path else None)
	return frappe_pack.findings(files, head, settings or {})


class TestFrappePackRules(UnitTestCase):
	def test_each_rule_fires_on_bad_code_and_not_on_good_code(self):
		for rule_id, path, bad, good in CASES:
			with self.subTest(rule_id, path=path):
				found = check(path, bad)
				self.assertIn(rule_id, [f.rule_id for f in found])
				# A body with `{{0}}` escapes is always formatted.
				self.assertFalse([f.body for f in found if "{{" in f.body or "}}" in f.body])
				self.assertNotIn(rule_id, [f.rule_id for f in check(path, good)])

	def test_the_finding_is_on_the_first_line_that_breaks_the_rule(self):
		code = 'def f():\n\tx = 1\n\tfrappe.throw("Not allowed")'
		found = check("app/utils.py", code)
		self.assertEqual(
			[(f.rule_id, f.start_line) for f in found], [("frappe-missing-translate-function-python", 3)]
		)

	def test_every_case_names_a_known_rule(self):
		known = {r.id for r in frappe_pack.RULES}
		cased = {c[0] for c in CASES} | {c[0] for c in OFF_CASES}
		self.assertEqual(cased - known, set())
		# Every rule has a test.
		self.assertEqual(known - cased, {"frappe-ignore-permissions"})

	def test_audit_rules_report_only_when_a_config_turns_them_on(self):
		for rule_id, path, code in OFF_CASES:
			with self.subTest(rule_id):
				self.assertNotIn(rule_id, [f.rule_id for f in check(path, code)])
				on = check(path, code, {"enabled_checks": [rule_id]})
				self.assertIn(rule_id, [f.rule_id for f in on])

	def test_a_config_turns_a_rule_off(self):
		code = "def f():\n\tfrappe.throw('Not allowed')"
		settings = {"disabled_checks": ["frappe-missing-translate-function-python"]}
		self.assertEqual(check("app/utils.py", code, settings), [])
		# Off wins over on.
		settings["enabled_checks"] = ["frappe-missing-translate-function-python"]
		self.assertEqual(check("app/utils.py", code, settings), [])

	def test_a_config_in_a_folder_turns_rules_on_for_the_files_under_it(self):
		code = "def f(args):\n\treturn args"
		files = {
			"guard.toml": 'disabled_checks = ["frappe-missing-translate-function-python"]',
			"legacy/.guard/config.toml": 'enabled_checks = ["overusing-args"]',
		}
		paths = ["legacy/x.py", "app/x.py"]
		config = ConfigResolver.from_files(None, files).resolve_pr(paths)
		self.assertEqual(config.errors, [])
		diffs = [d for path in paths for d in added(path, code)]
		found = frappe_pack.findings(
			diffs, lambda path: code, config.settings, lambda path: config.files[path].settings
		)
		self.assertEqual([f.path for f in found], ["legacy/x.py"])

	def test_an_unknown_check_is_noted_and_the_rest_of_the_config_holds(self):
		files = {"guard.toml": 'strictness = 3\nenabled_checks = ["frappe-no-such-rule", "overusing-args"]'}
		config = ConfigResolver.from_files(None, files).resolve_pr(["x.py"])
		self.assertEqual(len(config.errors), 1)
		self.assertIn("unknown check `frappe-no-such-rule`", config.errors[0])
		self.assertEqual(config.settings["strictness"], 3)
		self.assertEqual(config.settings["enabled_checks"], ["overusing-args"])

	def test_controller_fields_come_from_the_doctype_json(self):
		code = dedent(
			"""\
			class Note(Document):
				def on_update(self):
					self.title = "Done"
					self.helper_doc = frappe.get_doc("ToDo", self.todo)
					self._seen = True
			"""
		)
		path = "app/doctype/note/note.py"
		meta = json.dumps({"fields": [{"fieldname": "title"}, {"fieldname": "todo"}]})
		files = {path: code, "app/doctype/note/note.json": meta}
		with_meta = check(path, code, read=files.get)
		self.assertEqual([f.start_line for f in with_meta], [3])
		# Without the JSON, every attribute that is not private.
		self.assertEqual([f.start_line for f in check(path, code)], [3, 4])

	def test_only_controllers_lose_changes_after_save(self):
		code = "class Cache:\n\tdef on_update(self):\n\t\tself.value = 1"
		self.assertEqual(check("app/utils.py", code), [])
		# A value that is a literal does not prove that a later `db_set` saves this field.
		code = (
			"class Note(Document):\n\tdef on_update(self):\n\t\tself.title = 1\n\t\tself.db_set('other', 1)"
		)
		self.assertEqual([f.start_line for f in check("app/doctype/note/note.py", code)], [3])

	def test_a_rule_that_fails_loses_its_findings_not_the_review(self):
		from dataclasses import replace
		from unittest.mock import patch

		import frappe

		def fail(source):
			raise ValueError("odd tree")

		rules = [
			replace(r, check=fail) if r.id == "frappe-missing-translate-function-python" else r
			for r in frappe_pack.RULES
		]
		code = "def f(a):\n\tfrappe.throw('Not allowed')\n\tfrappe.db.sql(f'select {a}')"
		with (
			patch("guard.review.frappe_pack.engine.RULES", rules),
			patch.dict(frappe.flags, {"in_test": False}),
			patch.object(frappe, "log_error") as logged,
		):
			found = check("app/x.py", code)
		self.assertEqual([f.rule_id for f in found], ["frappe-sql-format-injection"])
		logged.assert_called_once()

	def test_line_numbers_count_only_newlines(self):
		# A form feed is a line break to `splitlines`, not to tree-sitter.
		code = (
			"x = 1\x0c\n# nosemgrep: frappe-sql-format-injection\ndef f(a):\n\tfrappe.db.sql(f'select {a}')"
		)
		self.assertEqual([f.start_line for f in check("app/x.py", code)], [4])

	def test_parses_python_with_new_syntax(self):
		# PEP 758: `except A, B:` without parentheses.
		code = "def f():\n\ttry:\n\t\tpass\n\texcept TypeError, ValueError:\n\t\tfrappe.throw('Bad')"
		self.assertEqual(len(check("app/utils.py", code)), 1)


class TestFrappePackEngine(UnitTestCase):
	def test_only_added_lines_report(self):
		head = "def f():\n\tfrappe.throw('Old')\n\tfrappe.throw('New')\n"
		diff = (
			"diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n@@ -1,2 +1,3 @@\n"
			" def f():\n \tfrappe.throw('Old')\n+\tfrappe.throw('New')\n"
		)
		found = frappe_pack.findings(parse_diff(diff), lambda path: head, {})
		self.assertEqual([f.start_line for f in found], [3])

	def test_a_change_inside_a_multi_line_match_reports_at_its_start(self):
		head = "def f(name):\n\tfrappe.db.sql(\n\t\tf'select {name}'\n\t)\n"
		diff = (
			"diff --git a/app/x.py b/app/x.py\n--- a/app/x.py\n+++ b/app/x.py\n@@ -1,4 +1,4 @@\n"
			" def f(name):\n \tfrappe.db.sql(\n-\t\t'select 1'\n+\t\tf'select {name}'\n \t)\n"
		)
		found = frappe_pack.findings(parse_diff(diff), lambda path: head, {})
		self.assertEqual([(f.rule_id, f.start_line) for f in found], [("frappe-sql-format-injection", 2)])

	def test_nosemgrep_covers_the_line_and_the_line_below(self):
		code = "\n".join(
			[
				"def f(a):",
				"\tfrappe.db.sql(f'select {a}')  # nosemgrep",
				"\t# nosemgrep: frappe-sql-format-injection",
				"\tfrappe.db.sql(f'select {a}')",
				"\tfrappe.db.sql(f'select {a}')  # nosemgrep: rules.frappe-sql-format-injection",
				"\tfrappe.db.sql(f'select {a}')  # nosemgrep: frappe-manual-commit",
			]
		)
		self.assertEqual([f.start_line for f in check("app/x.py", code)], [6])

	def test_one_finding_for_each_line(self):
		# A commit in a GET endpoint breaks two rules on one line.
		code = "@frappe.whitelist()\ndef f():\n\tfrappe.db.commit()"
		found = check("app/api.py", code)
		self.assertEqual([(f.rule_id, f.severity) for f in found], [("whitelisted-side-effect-on-get", "P1")])

	def test_skips_tests_deleted_minified_and_other_languages(self):
		bad = "def f():\n\tfrappe.throw('Not allowed')"
		self.assertEqual(check("app/tests/test_x.py", bad), [])
		self.assertEqual(check("app/x.min.js", 'frappe.msgprint("Saved");'), [])
		self.assertEqual(check("app/x.ts", 'frappe.msgprint("Saved");'), [])
		deleted = parse_diff(
			"diff --git a/app/x.py b/app/x.py\ndeleted file mode 100644\n--- a/app/x.py\n+++ /dev/null\n"
			"@@ -1,2 +0,0 @@\n-def f():\n-\tfrappe.throw('x')\n"
		)
		self.assertEqual(frappe_pack.findings(deleted, lambda path: None, {}), [])

	def test_without_the_head_it_reads_the_hunks(self):
		code = "def f():\n\tfrappe.throw('Not allowed')"
		found = frappe_pack.findings(added("app/x.py", code), lambda path: None, {})
		self.assertEqual([f.start_line for f in found], [2])

	def test_the_body_tells_how_to_mark_a_line_as_reviewed(self):
		[finding] = check("app/x.py", "def f():\n\tfrappe.throw('Not allowed')")
		self.assertIn("# nosemgrep: frappe-missing-translate-function-python", finding.body)
		[json_finding] = check("app/doctype/n/n.json", '{"permissions": [{"role": "All"}]}')
		self.assertNotIn("nosemgrep", json_finding.body)

	def test_line_numbers_survive_a_sweep_of_many_nodes(self):
		# tree-sitter 0.26 on Python 3.14 crashed on `start_point.row`. This walks many nodes.
		source = parse("app/x.py", "python", "x = list(map(str, y))\n" * 2000)
		self.assertEqual(len(list(frappe_pack.RULES[0].check(source))), 0)
		self.assertEqual(
			len(
				check(
					"app/x.py",
					"names = list(map(str, rows))\n" * 500,
					{"enabled_checks": ["frappe-no-functional-code"]},
				)
			),
			500,
		)
