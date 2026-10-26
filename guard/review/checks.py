"""Checks that need no model: secrets in the diff, the Frappe review pack, and project test rules."""

import math
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from guard.git.diff import FileDiff
from guard.indexing.languages import EXTENSIONS
from guard.review.findings import Finding
from guard.review.rule_files import ProjectRule

SECRET_PATTERNS = (
	("GitHub token", re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
	("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
	("Private key", re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
	("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
	("Stripe secret key", re.compile(r"\b(sk|rk)_live_[A-Za-z0-9]{20,}\b")),
	("OpenAI or Anthropic key", re.compile(r"\bsk-(ant-)?[A-Za-z0-9_-]{32,}\b")),
	("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
)
ASSIGNED_SECRET = re.compile(
	r"""(?i)\b(password|passwd|secret|api_?key|token|auth)\w*\s*[:=]\s*['"]([^'"\s]{16,})['"]"""
)
MEMBER_PATH = re.compile(r"[A-Za-z_$][\w$]*(?:\??\.[A-Za-z_$][\w$]*)+(?:\(\))?")
PLACEHOLDER = re.compile(
	r"(?i)example|placeholder|dummy|test|xxx|changeme|your[_-]|<|\$\{|\{\{|os\.environ|getenv"
)
TEST_FILE = re.compile(
	r"(^|/)(tests?|__tests__|fixtures?|spec)/|test_[^/]+$|_(test|spec)\.[a-z]+$|Tests?\.[a-z]+$|\.test\.|\.spec\."
)

FRAPPE_RULES = """Frappe review pack (the repository is a Frappe app):
- A @frappe.whitelist() method must check permissions (frappe.has_permission, doc.check_permission or frappe.only_for) before it reads or writes records for the caller.
- allow_guest=True endpoints must not expose records, and must validate every argument.
- ignore_permissions=True and flags.ignore_permissions need a comment that explains why the bypass is safe.
- frappe.db.sql must take values as parameters. Never build SQL with f-strings, % or + from input.
- Do not call frappe.get_doc, frappe.get_value or frappe.db.get_value in a loop over many records. Fetch in one query.
- A schema change that needs existing data changed must ship a patch in patches.txt.
- User-facing strings in Python and JavaScript must be wrapped in _() or __().
- frappe.db.commit() inside request handlers or document hooks is almost always wrong."""

# Each check fires on a line and then asks the code around it whether its rule applies. A check
# on the line alone posted 117 findings on suite#881, and the verifier refuted all of them: the
# endpoints checked access, and the commits and bypasses were in patches, jobs and helpers.
FRAPPE_LINE_CHECKS = (
	(
		"sql",
		re.compile(r"frappe\.db\.sql\(\s*f[\"']|frappe\.db\.sql\([^)]*[\"']\s*(%|\+|\.format\()"),
		"P1",
		"logic",
		"security",
		"SQL built from strings",
		"This query is built with string formatting. Pass values as parameters to `frappe.db.sql` so input cannot change the SQL.",
	),
	(
		"guest",
		re.compile(r"@frappe\.whitelist\([^)]*allow_guest\s*=\s*True"),
		"P1",
		"logic",
		"security",
		"New guest endpoint",
		"This endpoint is open to anyone without a login. Check that it exposes no records and validates every argument.",
	),
	(
		"bypass",
		re.compile(r"ignore_permissions\s*=\s*True"),
		"P2",
		"logic",
		"security",
		"Permission check is bypassed",
		"`ignore_permissions=True` skips the role checks. Confirm the caller is already checked, or remove it.",
	),
	(
		"commit",
		re.compile(r"^\s*frappe\.db\.commit\(\)"),
		"P2",
		"logic",
		"correctness",
		"Explicit commit",
		"Frappe commits at the end of the request. An explicit commit here can save half-finished work when a later step fails.",
	),
)
# Methods of a document that Frappe runs while a request saves, submits or deletes it.
DOC_HOOKS = frozenset(
	{
		"before_validate",
		"validate",
		"before_insert",
		"after_insert",
		"before_save",
		"on_update",
		"before_submit",
		"on_submit",
		"before_cancel",
		"on_cancel",
		"before_update_after_submit",
		"on_update_after_submit",
		"on_change",
		"on_trash",
		"after_delete",
		"before_rename",
		"after_rename",
	}
)
DEF_LINE = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)")
CLASS_LINE = re.compile(r"^\s*class\s")
WHITELIST = re.compile(r"@(?:frappe\.)?whitelist\b")
# A call that checks who the caller is: frappe.has_permission, doc.check_permission,
# frappe.only_for, and helpers of the app such as require_admin() or drive_check().
ACCESS_CHECK = re.compile(r"\b\w*(?:permission|require|_check|authori[sz]|only_for|has_role)\w*\s*\(", re.I)
# A line that the project marked as reviewed for its static analysis.
REVIEWED = re.compile(r"#\s*nosemgrep")
COMMENT = re.compile(r"(?:^|\s)#")
# Lines above a def that its decorators can take, and above a line that its comment can take.
DECORATOR_LINES = 6
COMMENT_LINES = 3


@dataclass
class Function:
	"""A Python function in the lines of a file, found by its indentation. Indexes start at 0."""

	name: str
	start: int
	body: int
	end: int
	decorators: str
	in_class: bool

	@property
	def whitelisted(self) -> bool:
		return bool(WHITELIST.search(self.decorators))

	@property
	def serves_request(self) -> bool:
		return self.whitelisted or (self.in_class and self.name in DOC_HOOKS)


def secret_findings(files: list[FileDiff]) -> list[Finding]:
	findings = []
	for diff in files:
		if TEST_FILE.search(diff.path):
			continue
		for hunk in diff.hunks:
			for line in hunk.lines:
				if line.kind != "+" or not line.new_no:
					continue
				kind = _secret_kind(line.text)
				if kind:
					findings.append(
						Finding(
							path=diff.path,
							start_line=line.new_no,
							end_line=line.new_no,
							severity="P0",
							comment_type="logic",
							category="security",
							title=f"{kind} committed to the repository",
							body=f"This line adds what looks like a {kind.lower()}. Anyone with read access can use it. Revoke it now, then load it from site config or the environment.",
							confidence=0.95,
							verified=True,
						)
					)
	return findings


def frappe_findings(files: list[FileDiff], head: Callable[[str], str | None]) -> list[Finding]:
	findings = []
	for diff in files:
		if not diff.path.endswith(".py") or TEST_FILE.search(diff.path):
			continue
		lines = str(head(diff.path) or "").splitlines()
		for hunk in diff.hunks:
			for line in hunk.lines:
				if line.kind != "+" or not line.new_no:
					continue
				for check, pattern, severity, comment_type, category, title, body in FRAPPE_LINE_CHECKS:
					if pattern.search(line.text) and _applies(check, lines, line.new_no - 1):
						findings.append(
							Finding(
								diff.path,
								line.new_no,
								line.new_no,
								severity,
								comment_type,
								title,
								body,
								category=category,
								confidence=0.75,
								rule_id="frappe-pack",
							)
						)
	return findings


def _applies(check: str, lines: list[str], index: int) -> bool:
	"""Whether the rule of a Frappe check holds at line `index` of the head.

	Without the source, the check fires on the line alone.
	"""
	if not 0 <= index < len(lines):
		return True
	if any(REVIEWED.search(line) for line in lines[max(index - 1, 0) : index + 1]):
		return False
	if check == "guest":
		# The rule: a guest endpoint must check access. Its body shows whether it does.
		function = _function_after(lines, index)
		return function is None or not ACCESS_CHECK.search("\n".join(lines[function.body : function.end]))
	if check == "bypass":
		# The rule: a bypass needs a comment that explains it. Only a whitelisted method lets a
		# caller reach it, and a check of the caller before the bypass makes it safe.
		function = _function_at(lines, index)
		if function is None or not function.whitelisted:
			return False
		return not ACCESS_CHECK.search("\n".join(lines[function.body : index])) and not _commented(
			lines, index
		)
	if check == "commit":
		# The rule: a commit is wrong in a request handler or a document hook. Patches, jobs and
		# scripts commit on purpose.
		function = _function_at(lines, index)
		return function is not None and function.serves_request
	return True


def _function_at(lines: list[str], index: int) -> Function | None:
	"""The function whose body holds line `index`. None at module level."""
	limit = _indent(lines[index])
	for number in range(index - 1, -1, -1):
		text = lines[number]
		if not text.strip() or text.lstrip().startswith("#") or _indent(text) >= limit:
			continue
		if DEF_LINE.match(text):
			return _function(lines, number)
		# An enclosing block, such as a loop or a call that spans lines.
		limit = _indent(text)
		if limit == 0:
			return None
	return None


def _function_after(lines: list[str], index: int) -> Function | None:
	"""The function that the decorator at line `index` belongs to."""
	for number in range(index, min(index + DECORATOR_LINES, len(lines))):
		if DEF_LINE.match(lines[number]):
			return _function(lines, number)
	return None


def _function(lines: list[str], start: int) -> Function:
	match = DEF_LINE.match(lines[start])
	assert match
	indent = len(match.group(1))
	decorators = []
	for number in range(start - 1, max(start - 1 - DECORATOR_LINES, -1), -1):
		text = lines[number]
		if not text.strip() or (_indent(text) <= indent and not text.lstrip().startswith(("@", ")", "#"))):
			break
		decorators.insert(0, text)
	# The body starts after the line that ends the signature, which can span lines.
	body = next(
		(n + 1 for n in range(start, len(lines)) if lines[n].split("#", 1)[0].rstrip().endswith(":")),
		start + 1,
	)
	end = next(
		(n for n in range(body, len(lines)) if lines[n].strip() and _indent(lines[n]) <= indent),
		len(lines),
	)
	in_class = False
	for number in range(start - 1, -1, -1):
		text = lines[number]
		if text.strip() and _indent(text) < indent and not text.lstrip().startswith(("@", "#")):
			in_class = bool(CLASS_LINE.match(text))
			break
	return Function(match.group(2), start, body, end, "\n".join(decorators), in_class)


def _commented(lines: list[str], index: int) -> bool:
	"""A comment on the line, or a comment line just above it or above the call that it is in."""
	if COMMENT.search(lines[index]):
		return True
	return any(line.lstrip().startswith("#") for line in lines[max(index - COMMENT_LINES, 0) : index])


def _indent(text: str) -> int:
	return len(text) - len(text.lstrip())


def missing_test_findings(
	files: list[FileDiff], written_paths: list[str], rules: list[ProjectRule]
) -> list[Finding]:
	"""A project rule asks for tests, code under it changed, and no test under it was added or updated."""
	tests = [path for path in written_paths if TEST_FILE.search(path)]
	for diff in files:
		if TEST_FILE.search(diff.path) or not diff.path.endswith(tuple(EXTENSIONS)):
			continue
		rule = next((r for r in rules if r.is_test_rule and r.covers(diff.path)), None)
		added = sorted(diff.added_lines())
		if rule is None or not added or any(rule.covers(path) for path in tests):
			continue
		return [
			Finding(
				diff.path,
				added[0],
				added[0],
				"P2",
				"logic",
				"Code changes without tests",
				f"This pull request changes code but adds or updates no test. `{rule.path}` line {rule.line}: {rule.text}",
				category="test-gap",
				confidence=0.8,
				rule_id=rule.id,
			)
		]
	return []


def _secret_kind(text: str) -> str | None:
	for kind, pattern in SECRET_PATTERNS:
		if pattern.search(text):
			return kind
	match = ASSIGNED_SECRET.search(text)
	if not match or PLACEHOLDER.search(text) or entropy(match.group(2)) <= 3.5:
		return None
	if _is_expression(match.group(2)) or _is_bound_attribute(text, match.start()):
		return None
	return "Hard-coded secret"


def _is_expression(value: str) -> bool:
	"""`connectionState.guestAuthToken` names a variable. A JWT is dotted too, but starts `eyJ`."""
	return bool(MEMBER_PATH.fullmatch(value)) and not value.startswith("eyJ")


def _is_bound_attribute(text: str, start: int) -> bool:
	"""`:guest-auth-token="..."` in a Vue template: the value is code, not a string."""
	attribute = text[:start].rsplit(None, 1)[-1] if text[:start].strip() else ""
	return attribute.startswith((":", "v-bind:", "@", "v-on:"))


def entropy(value: str) -> float:
	counts = Counter(value)
	return -sum(c / len(value) * math.log2(c / len(value)) for c in counts.values()) if value else 0.0


def mask_secrets(text: str) -> str:
	"""Hide secret values before text goes to the model."""
	for _, pattern in SECRET_PATTERNS:
		text = pattern.sub("[secret]", text)
	return ASSIGNED_SECRET.sub(
		lambda m: (
			m.group(0).replace(m.group(2), "[secret]")
			if not PLACEHOLDER.search(m.group(0)) and entropy(m.group(2)) > 3.5
			else m.group(0)
		),
		text,
	)
