"""Run the rules of the Frappe review pack on the lines that a pull request adds.

A rule reads the syntax tree of the whole file at the head, so it sees the function, the class and
the imports around a line. It reports only a match on an added line: old code is not the author's.
"""

import re
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

import frappe
import tree_sitter as ts

from guard.git.diff import FileDiff
from guard.indexing.parser import _parser
from guard.review.checks import TEST_FILE
from guard.review.findings import Finding
from guard.utils.redact import redact

# The language of each file that the pack reads. Vue and TypeScript front ends use frappe-ui, not the
# desk API that the JavaScript rules know.
LANGUAGES = {".py": "python", ".js": "javascript", ".json": "json"}
# A comment that marks a line as reviewed, as Semgrep reads it: bare, or with the ids it covers.
NOSEMGREP = re.compile(r"nosemgrep(?::\s*([\w.,\s-]+))?")
IDS = re.compile(r"[,\s]+")
COMMENT_PREFIX = {"python": "#", "javascript": "//"}
# Guard is unsure of a match more often than of a secret: the rule cannot see what calls the code.
CONFIDENCE = 0.75

Check = Callable[["Source"], Iterable["Hit"]]


@dataclass(frozen=True)
class Rule:
	"""One check, named by the id of its Semgrep rule in frappe/semgrep-rules.

	`on` is False for a rule that flags code to audit, not code that is wrong. Such a rule runs only
	when a config names it in `enabled_checks`.
	"""

	id: str
	language: str
	severity: str
	category: str
	title: str
	body: str
	check: Check
	on: bool = True
	comment_type: str = "logic"
	paths: Callable[[str], bool] | None = None
	tests: bool = False  # the rule reads test files too


@dataclass
class Hit:
	"""A match. It reports at `line`, and it is new when the change added a line in `lines`."""

	line: int
	lines: set[int]
	values: dict[str, str] = field(default_factory=dict)

	@classmethod
	def at(cls, node: ts.Node, *also: ts.Node, **values: Any) -> "Hit":
		lines: set[int] = set()
		for part in (node, *also):
			lines.update(range(line_of(part), part.end_point[0] + 2))
		return cls(line_of(node), lines, {k: str(v) for k, v in values.items()})


def line_of(node: ts.Node) -> int:
	"""The line that a node starts on, from 1. Not `start_point.row`: on Python 3.14 that attribute of
	tree-sitter 0.26 corrupts memory, and a later call crashes the worker."""
	return node.start_point[0] + 1


@dataclass
class Source:
	path: str
	language: str
	text: str
	tree: ts.Tree
	lines: list[str]
	# Reads another file at the head, such as the DocType JSON of a controller.
	read: Callable[[str], str | None] = lambda path: None

	@property
	def root(self) -> ts.Node:
		return self.tree.root_node

	@property
	def is_test(self) -> bool:
		return bool(TEST_FILE.search(self.path))


RULES: list[Rule] = []


def rule(
	rule_id: str,
	language: str,
	severity: str,
	category: str,
	title: str,
	body: str,
	on: bool = True,
	comment_type: str = "logic",
	paths: Callable[[str], bool] | None = None,
	tests: bool = False,
) -> Callable[[Check], Check]:
	"""Add the decorated check to RULES."""

	def add(check: Check) -> Check:
		RULES.append(
			Rule(rule_id, language, severity, category, title, body, check, on, comment_type, paths, tests)
		)
		return check

	return add


def active(settings: dict[str, Any]) -> list[Rule]:
	"""The rules that the config leaves on. A rule in `disabled_checks` stays off, even when a config
	names it in `enabled_checks` too."""
	enabled = set(settings.get("enabled_checks") or [])
	disabled = set(settings.get("disabled_checks") or [])
	return [r for r in RULES if (r.on or r.id in enabled) and r.id not in disabled]


def run(
	files: list[FileDiff],
	head: Callable[[str], str | None],
	settings: dict[str, Any],
	settings_for: Callable[[str], dict[str, Any] | None] = lambda path: None,
) -> list[Finding]:
	"""`settings_for(path)` gives the config of one file, so that a config in a folder turns rules on
	and off for the files under it. Without it, `settings` holds for every file."""
	findings: list[Finding] = []
	for diff in files:
		language = language_of(diff.path)
		added = diff.added_lines()
		if not language or diff.status == "deleted" or diff.is_binary or not added:
			continue
		rules = active(settings_for(diff.path) or settings)
		own = [r for r in rules if r.language == language and _reads(r, diff.path)]
		if not own:
			continue
		source = parse(diff.path, language, head(diff.path) or _from_hunks(diff), head)
		findings += _best_per_line(f for r in own for f in _safely(r, source, added))
	return findings


def _safely(rule: Rule, source: Source, added: set[int]) -> list[Finding]:
	"""The findings of one rule. A rule that fails on an odd tree loses its findings, not the review."""
	try:
		return list(_findings(rule, source, added))
	except Exception:
		if frappe.flags.in_test:
			raise
		frappe.log_error(redact(frappe.get_traceback()), f"Guard check {rule.id} on {source.path}")
		return []


def language_of(path: str) -> str | None:
	if path.endswith(".min.js"):
		return None
	return next((lang for ext, lang in LANGUAGES.items() if path.endswith(ext)), None)


def parse(
	path: str, language: str, text: str, read: Callable[[str], str | None] = lambda path: None
) -> Source:
	tree = _parser(language).parse(text.encode())
	# Not `splitlines()`: it also splits on form feeds and U+2028, and tree-sitter counts only `\n`.
	return Source(path, language, text, tree, text.split("\n"), read)


def _reads(rule: Rule, path: str) -> bool:
	if not rule.tests and TEST_FILE.search(path):
		return False
	return rule.paths is None or rule.paths(path)


def _findings(rule: Rule, source: Source, added: set[int]) -> Iterator[Finding]:
	seen: set[int] = set()
	for hit in rule.check(source):
		if hit.line in seen or not hit.lines & added or _reviewed(source, hit.line, rule.id):
			continue
		seen.add(hit.line)
		yield Finding(
			source.path,
			hit.line,
			hit.line,
			rule.severity,
			rule.comment_type,
			rule.title,
			_body(rule, source.language, hit.values),
			category=rule.category,
			confidence=CONFIDENCE,
			rule_id=rule.id,
		)


def _body(rule: Rule, language: str, values: dict[str, str]) -> str:
	body = rule.body.format(**values)
	prefix = COMMENT_PREFIX.get(language)
	if not prefix:
		return body
	return (
		f"{body}\n\nIf this is on purpose, add `{prefix} nosemgrep: {rule.id}` to the line, with the reason."
	)


def _best_per_line(findings: Iterable[Finding]) -> list[Finding]:
	"""One finding for each line: the most severe, then the first rule. Two rules on one call, such as
	a commit in a GET endpoint, say one thing twice."""
	best: dict[int, Finding] = {}
	for finding in findings:
		current = best.get(finding.start_line)
		if current is None or finding.severity < current.severity:
			best[finding.start_line] = finding
	return sorted(best.values(), key=lambda f: f.start_line)


def _reviewed(source: Source, line: int, rule_id: str) -> bool:
	"""A `nosemgrep` comment on the line, or on a comment line just above it, covers the rule. A
	comment at the end of the line above covers only that line."""
	above = source.lines[line - 2 : line - 1] if line >= 2 else []
	for text in [t for t in above if t.lstrip().startswith(("#", "//"))] + source.lines[line - 1 : line]:
		match = NOSEMGREP.search(text)
		if not match:
			continue
		if not match.group(1):
			return True
		# Semgrep ids can carry the path of the rule file: `rules.frappe-manual-commit`.
		ids = IDS.split(match.group(1).strip())
		if any(i == rule_id or i.endswith(f".{rule_id}") for i in ids):
			return True
	return False


def _from_hunks(diff: FileDiff) -> str:
	"""The head lines that the diff shows, each at its own line number, when the head cannot be read.
	The parser recovers from the gaps."""
	lines: dict[int, str] = {}
	for hunk in diff.hunks:
		for line in hunk.lines:
			if line.kind != "-" and line.new_no:
				lines[line.new_no] = line.text
	return "\n".join(lines.get(n, "") for n in range(1, max(lines, default=0) + 1))
