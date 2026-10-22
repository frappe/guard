from frappe.tests import UnitTestCase

from guard.evals import run as evals
from guard.git.diff import parse_diff
from guard.review import checks
from guard.review.findings import Finding, FindingFilter
from guard.review.rule_files import collect, mandatory_rules

RULES = """# Agent Guide

## Testing

- Always add or update tests for behavior changes.
- Prefer small functions.
- Never commit secrets.

```python
# must not count: code block
```
"""

DIFF = """diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -1,2 +1,4 @@
 def top():
-    return 1
+    value = compute()
+    return value
"""


def finding(severity="P2", line=2, rule_id=None):
	return Finding("app/a.py", line, line, severity, "logic", "Value is vague", "x", rule_id=rule_id)


class TestProjectRules(UnitTestCase):
	def test_mandatory_lines_get_ids(self):
		rules = mandatory_rules([("CLAUDE.md", RULES)])
		self.assertEqual([r.id for r in rules], ["project:CLAUDE.md:5", "project:CLAUDE.md:7"])
		self.assertEqual(rules[0].text, "Always add or update tests for behavior changes.")
		self.assertEqual([r.is_test_rule for r in rules], [True, False])

	def test_missing_tests(self):
		rules = mandatory_rules([("CLAUDE.md", RULES)])
		files = parse_diff(DIFF)
		[missing] = checks.missing_test_findings(files, ["app/a.py"], rules)
		self.assertEqual((missing.path, missing.start_line, missing.severity), ("app/a.py", 2, "P1"))
		self.assertEqual(missing.rule_id, "project:CLAUDE.md:5")
		self.assertFalse(checks.missing_test_findings(files, ["app/a.py", "app/tests/test_a.py"], rules))
		self.assertFalse(checks.missing_test_findings(files, ["app/a.py"], []))
		docs = parse_diff(DIFF.replace("app/a.py", "docs/a.md"))
		self.assertFalse(checks.missing_test_findings(docs, ["docs/a.md"], rules))


class TestRuleFilter(UnitTestCase):
	def test_project_rule_is_at_least_p1(self):
		rule = finding("P2", rule_id="project:CLAUDE.md:5")
		other = finding("P2", rule_id="frappe-pack")
		FindingFilter(parse_diff(DIFF), {"strictness": 3}).run([rule, other])
		self.assertEqual((rule.severity, rule.kept), ("P1", True))
		self.assertFalse(other.kept)

	def test_rereview_keeps_p2_only_on_new_lines(self):
		new, old = finding(line=2), finding(line=1)
		FindingFilter(parse_diff(DIFF), {"strictness": 1}, fresh_lines={"app/a.py": {2}}).run([new, old])
		self.assertTrue(new.kept)
		self.assertEqual(old.status_reason, "re-reviews post P2 only on new lines")

	def test_first_review_keeps_every_p2(self):
		nit = finding(line=1)
		FindingFilter(parse_diff(DIFF), {"strictness": 1}).run([nit])
		self.assertTrue(nit.kept)


class TestEvalCases(UnitTestCase):
	def test_pilot_678_flags_missing_tests_without_a_model(self):
		[case] = evals.load_cases("pilot-678")
		payload = case["payload"]
		files = parse_diff(payload["diff"])
		self.assertTrue(all(f.path in payload["files"] for f in files))
		found = collect([f.path for f in files], payload["files"].get)
		missing = checks.missing_test_findings(files, [f.path for f in files], mandatory_rules(found))
		expected = [e for e in case["expected"] if e["keywords"] == ["test"]]
		self.assertTrue(evals.matches(expected[0], vars(missing[0])))

	def test_matches(self):
		expected = {"path": "a.ts", "keywords": ["port", "fail"]}
		self.assertTrue(evals.matches(expected, {"path": "a.ts", "title": "Failed load", "body": "port 0"}))
		self.assertFalse(evals.matches(expected, {"path": "b.ts", "title": "Failed load", "body": "port 0"}))
		self.assertFalse(evals.matches(expected, {"path": "a.ts", "title": "Failed load", "body": ""}))
