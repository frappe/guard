import ast

from frappe.tests import UnitTestCase

from guard.git.diff import parse_diff
from guard.review import rule_checks
from guard.review.rule_files import ProjectRule

RULES = [
	ProjectRule("CLAUDE.md", 50, "Keep cyclomatic complexity <= 3"),
	ProjectRule("CLAUDE.md", 51, "Keep files between 100 and 20 lines when practical."),
	ProjectRule("CLAUDE.md", 52, "Name boolean-returning properties and methods with `is_` or `has_`."),
]

BASE = """class Builder:
    def build(self, app):
        if app.ready:
            return 1
        return 0
"""

HEAD = """class Builder:
    def build(self, app):
        if app.ready and app.studio:
            return 1
        for item in app.items:
            if item:
                return 2
        return 0

    def _needs_replacement(self) -> bool:
        return True

    def is_ready(self) -> bool:
        return True

    def restore(self) -> bool:
        return True
"""


def diff_of(path: str, old: str, new: str) -> str:
	"""A unified diff that replaces every line, so every head line is added."""
	old_lines, new_lines = old.splitlines(), new.splitlines()
	body = [f"-{line}" for line in old_lines] + [f"+{line}" for line in new_lines]
	header = f"@@ -1,{len(old_lines)} +1,{len(new_lines)} @@"
	return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n{header}\n" + "\n".join(body) + "\n"


def check(path: str, old: str, new: str, rules=RULES):
	files = parse_diff(diff_of(path, old, new))
	return rule_checks.findings(files, {path: new}.get, {path: old}.get, rules)


class TestRuleChecks(UnitTestCase):
	def test_complexity_counts_branches_and_operands(self):
		node = ast.parse(HEAD).body[0].body[0]  # type: ignore[attr-defined]
		# 1, +1 if, +1 and, +1 for, +1 if
		self.assertEqual(rule_checks.complexity(node), 5)

	def test_a_change_that_raises_complexity_past_the_limit(self):
		# pilot#685: "raises the cyclomatic complexity of build_assets_for_app from 7 to 9".
		found = [f for f in check("app/builder.py", BASE, HEAD) if "complexity" in f.title]
		self.assertEqual(len(found), 1)
		self.assertEqual((found[0].severity, found[0].rule_id), ("P2", "project:CLAUDE.md:50"))
		self.assertIn("complexity 5 (was 2)", found[0].body)
		self.assertIn("`CLAUDE.md` line 50 asks: Keep cyclomatic complexity <= 3", found[0].body)

	def test_a_function_that_was_already_complex_and_did_not_grow(self):
		self.assertFalse(
			[f for f in check("app/builder.py", HEAD, HEAD + "\n# note\n") if "complexity" in f.title]
		)

	def test_a_new_boolean_method_without_its_prefix(self):
		# pilot#692: "_needs_replacement() returns a boolean ... rename it to _is_replacement_needed()".
		found = [f for f in check("app/builder.py", BASE, HEAD) if f.title.startswith("Boolean")]
		self.assertEqual(
			[f.title for f in found], ["Boolean `_needs_replacement` does not start with `is_` or `has_`"]
		)

	def test_a_file_that_grows_past_the_line_limit(self):
		# pilot#663: "The added GitHub panel pushes SetupChecklist.vue to 544 lines".
		old, new = "x = 1\n" * 15, "x = 1\n" * 25
		found = [f for f in check("app/panel.py", old, new) if "line limit" in f.title]
		self.assertEqual([f.title for f in found], ["File grows past the 20-line limit"])
		self.assertEqual(found[0].start_line, 21)
		# A file that was over the limit before is not this change's doing.
		self.assertFalse([f for f in check("app/panel.py", new, new + "y = 2\n") if "line limit" in f.title])

	def test_no_rule_no_check(self):
		self.assertEqual(check("app/builder.py", BASE, HEAD, rules=[]), [])
