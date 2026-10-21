import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase

from guard.config import gate
from guard.config.globs import match_filter, match_path
from guard.config.resolver import ConfigResolver, merge_auto_approve, tier_for
from guard.config.schema import Layer

ROOT = """
strictness = 2
auto_review = ["open", "push"]
exclude_authors = ["dependabot[bot]"]
instructions = "Root instructions."

[[rules]]
id = "no-console"
rule = "Do not use console.log."
severity = "medium"

[[files]]
path = "docs/architecture.md"
description = "Architecture"

[auto_approve]
enabled = true
risk_ceiling = "medium"

[auto_approve.filters]
exclude_paths = ["src/auth/**"]
"""

DB = """
strictness = 1
effort = "apex"
disabled_rules = ["no-console"]
ignore_patterns = ["generated/**"]

[[rules]]
rule = "Use parameterized queries."
scope = ["queries/**"]

[auto_approve]
enabled = true
risk_ceiling = "low"
"""

FILES = {
	".guard/config.toml": ROOT,
	".guard/rules.md": "## Errors\nHandle every error.",
	"guard.toml": "strictness = 3",  # ignored: .guard/ wins in the same directory
	"packages/db/.guard/config.toml": DB,
	"packages/db/.guard/files.toml": '[[files]]\npath = "schema.sql"\ndescription = "Schema"\nscope = ["queries/**"]\n',
	"packages/api/guard.toml": 'instructions = "API instructions."\nexclude_authors = ["renovate[bot]"]\n[sections.summary]\ncollapsible = true\n',
	"packages/bad/.guard/config.toml": "strictness = 9",
	"packages/broken/guard.toml": "strictness = ",
}


def resolver(files=FILES, base=None):
	return (
		ConfigResolver.from_files(None, files)
		if base is None
		else ConfigResolver(None, files.get, list(files), base_layers=base)
	)


class TestGlobs(UnitTestCase):
	def test_filter_globs(self):
		self.assertTrue(match_filter("dependabot[bot]", ["dependabot[bot]"]))
		self.assertTrue(match_filter("Release/1.2", ["release/*"]))
		self.assertFalse(match_filter("release/1/2", ["release/*"]))
		self.assertTrue(match_filter("release/1/2", ["release/**"]))
		self.assertTrue(match_filter("wip-test", ["{wip,draft}-*"]))
		self.assertTrue(match_filter("ci-bot", ["*-bot"]))

	def test_path_globs(self):
		self.assertTrue(match_path("db/migrations/001.sql", ["db/migrations"]))
		self.assertTrue(match_path("a/b/c.generated.ts", "**/*.generated.*"))
		self.assertFalse(match_path("src/app.ts", ["dist/**"]))


class TestResolver(UnitTestCase):
	def setUp(self):
		self.resolver = resolver(base=[])

	def test_root(self):
		config = self.resolver.resolve("src/app.ts")
		self.assertEqual(config.settings["strictness"], 2)
		self.assertEqual(config.settings["auto_review"], ["open", "push"])
		self.assertEqual([r.id for r in config.rules], ["no-console"])
		self.assertEqual([f.path for f in config.files], ["docs/architecture.md"])
		self.assertEqual(config.rules_md[0][1], "## Errors\nHandle every error.")
		self.assertEqual(config.provenance["strictness"], ".guard/config.toml")

	def test_nested_cascade(self):
		config = self.resolver.resolve("packages/db/queries/users.py")
		self.assertEqual(config.settings["strictness"], 1)
		self.assertEqual(config.settings["auto_review"], ["open", "push"])
		self.assertEqual([r.rule for r in config.rules], ["Use parameterized queries."])
		self.assertEqual([f.path for f in config.files], ["docs/architecture.md", "packages/db/schema.sql"])
		self.assertEqual(config.efforts, ["apex"])

	def test_scope_and_ignore(self):
		self.assertEqual([r.rule for r in self.resolver.resolve("packages/db/other.py").rules], [])
		self.assertTrue(self.resolver.resolve("packages/db/generated/x.py").ignored)
		self.assertFalse(self.resolver.resolve("generated/x.py").ignored)

	def test_single_file_and_sections(self):
		config = self.resolver.resolve("packages/api/handler.py")
		self.assertEqual([t for _, t in config.instructions], ["Root instructions.", "API instructions."])
		self.assertEqual(
			config.settings["sections"]["summary"],
			{"included": True, "collapsible": True, "default_open": False},
		)

	def test_skip_lists_add_up(self):
		authors = self.resolver.resolve("packages/api/handler.py").settings["exclude_authors"]
		self.assertIn("dependabot[bot]", authors)
		self.assertIn("renovate[bot]", authors)

	def test_errors(self):
		errors = "\n".join(self.resolver.errors)
		self.assertIn("packages/bad/.guard/config.toml: strictness must be 1, 2 or 3", errors)
		self.assertIn("packages/broken/guard.toml: invalid TOML", errors)
		self.assertEqual(self.resolver.resolve("packages/bad/x.py").settings["strictness"], 2)

	def test_pull_request_merge(self):
		config = self.resolver.resolve_pr(
			["src/app.ts", "packages/db/queries/users.py", "packages/db/generated/x.py"]
		)
		self.assertEqual(config.settings["strictness"], 2)
		self.assertEqual(config.efforts, ["apex"])
		self.assertEqual(config.reviewed_paths(), ["src/app.ts", "packages/db/queries/users.py"])
		self.assertEqual(tier_for(config.efforts, [], None), "apex")

	def test_auto_approve_strictest(self):
		policy = self.resolver.auto_approve_policy(["packages/db/x.py"])
		self.assertTrue(policy["enabled"])
		self.assertEqual(policy["risk_ceiling"], "low")
		self.assertEqual(policy["filters"]["exclude_paths"], ["src/auth/**"])
		merged = merge_auto_approve(
			[
				{"enabled": True, "filters": {"include_paths": ["a", "b"]}},
				{"enabled": False, "filters": {"include_paths": ["b"]}},
			]
		)
		self.assertFalse(merged["enabled"])
		self.assertEqual(merged["filters"]["include_paths"], ["b"])

	def test_dashboard_layer_is_lowest(self):
		base = Layer(source="dashboard", settings={"strictness": 3, "review_drafts": True})
		config = resolver(base=[base]).resolve("src/app.ts")
		self.assertEqual(config.settings["strictness"], 2)
		self.assertTrue(config.settings["review_drafts"])


class TestGate(UnitTestCase):
	def facts(self, **kwargs):
		values = {
			"repository": "acme/app",
			"title": "Fix bug",
			"description": "",
			"author": "ada",
			"base_branch": "main",
			"head_branch": "fix",
			"paths": ["src/a.py"],
			"additions": 10,
		}
		values.update(kwargs)
		return gate.PullRequestFacts(**values)

	def setUp(self):
		self.config = resolver(base=[]).resolve_pr(["src/a.py"])

	def test_events_and_drafts(self):
		self.assertTrue(gate.check(self.facts(), self.config, "push", False, []).allowed)
		self.assertFalse(gate.check(self.facts(), self.config, "rebase", False, []).allowed)
		self.assertFalse(gate.check(self.facts(is_draft=True), self.config, "open", False, []).allowed)
		self.assertTrue(gate.check(self.facts(is_draft=True), self.config, "open", True, []).allowed)

	def test_filters(self):
		result = gate.check(self.facts(author="dependabot[bot]"), self.config, "open", False, [])
		self.assertEqual(result.reason, "author is excluded")
		self.assertFalse(
			gate.check(self.facts(author="dependabot[bot]"), self.config, "open", True, []).allowed
		)

	def test_dashboard_rules(self):
		only_main = gate.FilterRule(
			"main only", [{"field": "target_branch", "operator": "include", "values": ["main"]}]
		)
		apex_big = gate.FilterRule(
			"big", [{"field": "files_changed", "operator": "more_than", "values": ["1"]}], "apex"
		)
		self.assertTrue(gate.check(self.facts(), self.config, "open", False, [only_main]).allowed)
		self.assertFalse(
			gate.check(self.facts(base_branch="dev"), self.config, "open", False, [only_main]).allowed
		)
		result = gate.check(self.facts(paths=["a", "b"]), self.config, "open", False, [only_main, apex_big])
		self.assertEqual(result.tier_rules, ["apex"])

	def test_tiers(self):
		self.assertEqual(tier_for(["plus"], [], None), "plus")
		self.assertEqual(tier_for(["plus"], [], "apex"), "apex")
		self.assertEqual(tier_for(["apex"], [], "base"), "apex")
		self.assertEqual(tier_for(["plus"], ["auto"], None), "auto")
		self.assertEqual(tier_for(["plus"], ["auto"], "apex"), "apex")
		self.assertEqual(gate.auto_tier(self.facts()), "base")
		self.assertEqual(gate.auto_tier(self.facts(paths=["src/auth/login.py"], additions=300)), "plus")
		self.assertEqual(
			gate.auto_tier(self.facts(paths=[f"f{i}" for i in range(40)], additions=900)), "apex"
		)

	def test_auto_approve_filters(self):
		ok, reason = gate.auto_approve_filters_pass(
			{"exclude_paths": ["src/auth/**"]}, self.facts(paths=["src/auth/x.py"])
		)
		self.assertEqual(reason, "touches an excluded path")
		self.assertFalse(ok)
		ok, _ = gate.auto_approve_filters_pass(
			{"include_paths": ["docs/**"], "exclude_authors": ["bot"]}, self.facts(paths=["docs/a.md"])
		)
		self.assertTrue(ok)


class TestDashboardLayers(IntegrationTestCase):
	def test_custom_context_scope(self):
		frappe.get_doc(
			{
				"doctype": "Custom Context",
				"context_type": "Rule",
				"title": "acme only",
				"body": "Acme rule",
				"repository_scope": "acme/*",
				"rule_id": "acme-rule",
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "Custom Context",
				"context_type": "Rule",
				"title": "other",
				"body": "Other rule",
				"repository_scope": "other/*",
			}
		).insert()
		frappe.get_doc(
			{
				"doctype": "Custom Context",
				"context_type": "Rule",
				"title": "py",
				"body": "Python rule",
				"path_scope": "**/*.py",
			}
		).insert()
		from guard.config import dashboard

		layer = dashboard.context_layer("acme/app", None)
		rules = {r.rule: r for r in layer.rules}
		self.assertIn("Acme rule", rules)
		self.assertNotIn("Other rule", rules)
		files = {"guard.toml": 'disabled_rules = ["acme-rule"]'}
		config = ConfigResolver(None, files.get, list(files), base_layers=[layer]).resolve("x.py")
		self.assertEqual([r.rule for r in config.rules], ["Python rule"])
