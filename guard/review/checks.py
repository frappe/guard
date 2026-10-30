"""Checks that need no model: secrets in the diff, the Frappe review pack, and project test rules."""

import math
import re
from collections import Counter

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
