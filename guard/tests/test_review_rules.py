from frappe.tests import UnitTestCase

from guard.evals import run as evals
from guard.git.diff import parse_diff
from guard.review import checks, verify
from guard.review.findings import Finding, FindingFilter
from guard.review.rule_files import ProjectRule, collect, mandatory_rules
from guard.review.run import ReviewRun

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

	def test_only_rules_that_ask_for_tests(self):
		def asks(text):
			return ProjectRule("CLAUDE.md", 1, text).is_test_rule

		self.assertTrue(asks("You must write tests for new endpoints."))
		self.assertFalse(asks("Always run the tests before committing."))
		self.assertFalse(asks("Never add flaky tests."))

	def test_rule_covers_its_directory(self):
		self.assertTrue(ProjectRule("CLAUDE.md", 1, "x").covers("app/a.py"))
		self.assertTrue(ProjectRule("app/AGENTS.md", 1, "x").covers("app/a.py"))
		self.assertFalse(ProjectRule("web/AGENTS.md", 1, "x").covers("app/a.py"))
		self.assertTrue(ProjectRule(".cursor/rules/x.md", 1, "x").covers("app/a.py"))

	def test_test_file_names(self):
		for path in (
			"pkg/a_test.go",
			"spec/models/user_spec.rb",
			"src/FooTest.java",
			"a.test.ts",
			"test_a.py",
		):
			self.assertTrue(checks.TEST_FILE.search(path), path)
		self.assertFalse(checks.TEST_FILE.search("app/latest.py"))

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

	def test_missing_tests_respects_rule_directory(self):
		web = [ProjectRule("web/AGENTS.md", 1, "Always add tests.")]
		files = parse_diff(DIFF)
		self.assertFalse(checks.missing_test_findings(files, ["app/a.py"], web))
		app = [ProjectRule("app/AGENTS.md", 1, "Always add tests.")]
		self.assertTrue(checks.missing_test_findings(files, ["app/a.py", "web/a.test.ts"], app))
		self.assertFalse(checks.missing_test_findings(files, ["app/a.py", "app/a_test.py"], app))


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

	def test_a_confirmed_p2_is_not_dropped_for_its_confidence(self):
		# pilot#663: the verifier confirmed "no provider picker" at 0.55, and strictness 2 dropped it.
		confirmed, unchecked = finding(line=1), finding(line=2)
		confirmed.title = "Storage step has no provider picker"
		for item in (confirmed, unchecked):
			item.confidence = 0.55
		confirmed.verified = True
		FindingFilter(parse_diff(DIFF), {"strictness": 2}).run([confirmed, unchecked])
		self.assertTrue(confirmed.kept)
		self.assertEqual(unchecked.status_reason, "low-confidence P2 at strictness 2")

	def test_one_comment_for_an_issue_on_many_lines(self):
		# pilot#690 got one P1 three times, and suite#881 one check 108 times.
		repeats = [finding("P1", line=n) for n in (1, 2, 3)]
		for other, path in zip(repeats[1:], ("app/b.py", "app/c.py"), strict=True):
			other.path = path
		lone = finding("P1", line=2)
		lone.title = "Another issue"
		FindingFilter(parse_diff(DIFF), {"strictness": 1}).run([*repeats, lone])
		self.assertEqual([f.kept for f in repeats], [True, False, False])
		self.assertIn("- `app/b.py:2`", repeats[0].body)
		self.assertIn("- `app/c.py:3`", repeats[0].body)
		self.assertEqual(repeats[1].status_reason, "same issue as app/a.py:1, listed in its comment")
		self.assertTrue(lone.kept)


class TestVerifier(UnitTestCase):
	def test_project_rule_text_goes_to_the_verifier(self):
		run = ReviewRun(None, None, "", lambda path: "x = 1\n", lambda path: None, lambda path: [], "", "")
		item = verify._item(run, 0, finding(rule_id="project:CLAUDE.md:5"), "Always add tests.")
		self.assertIn("Always add tests.", item)
		self.assertNotIn("mandatory project rule", verify._item(run, 0, finding()))

	def test_the_verifier_sees_the_code_a_finding_cites(self):
		# grove#73: the evidence was in an old patch, and the verifier saw only app code.
		patch = "\n".join(f"line {n}" for n in range(1, 60)).replace(
			"line 45", 'frappe.db.exists("Central Team")'
		)
		files = {"app/a.py": "x = 1\n", "grove/patches/v1_0/move.py": patch}
		run = ReviewRun(None, None, "", files.get, lambda path: None, lambda path: [], "", "")
		claim = finding("P1")
		claim.body = "`grove/patches/v1_0/move.py:45,50` still inserts the removed `Central Team` doctype."
		cited = verify._cited(run, claim)
		self.assertIn("cited code grove/patches/v1_0/move.py", cited)
		self.assertIn('frappe.db.exists("Central Team")', cited)
		# The lines around the finding itself are shown already.
		claim.body = "See `app/a.py:2`."
		self.assertEqual(verify._cited(run, claim), "")

	def test_the_verifier_sees_its_earlier_verdict(self):
		# pilot#687: one review dismissed a claim, and the next posted the same claim as a P1.
		claim = finding("P1")
		claim.title = "Failed backup leaves already-uploaded files in S3"
		rows = [
			{
				"path": "app/a.py",
				"title": "Failed backup leaves uploaded files in S3",
				"status_reason": "verifier: every upload aborts first",
			}
		]
		block = verify._earlier(claim, rows, 0)
		self.assertIn("Dismissed then because: every upload aborts first", block)
		self.assertEqual(verify._earlier(claim, [{**rows[0], "path": "app/b.py"}], 0), "")


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
		expected = {"path": "a.ts", "severity": "P1", "keywords": ["port", "fail"]}
		found = {"path": "a.ts", "severity": "P1", "title": "Failed load", "body": "port 0"}
		self.assertTrue(evals.matches(expected, found))
		self.assertTrue(evals.matches(expected, {**found, "severity": "P0"}))
		self.assertFalse(evals.matches(expected, {**found, "severity": "P2"}))
		self.assertFalse(evals.matches(expected, {**found, "path": "b.ts"}))
		self.assertFalse(evals.matches(expected, {**found, "body": ""}))
