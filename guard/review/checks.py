"""Checks that need no model: secrets in the diff, and the Frappe review pack."""

import math
import re
from collections import Counter

from guard.git.diff import FileDiff
from guard.review.findings import Finding

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
PLACEHOLDER = re.compile(
	r"(?i)example|placeholder|dummy|test|xxx|changeme|your[_-]|<|\$\{|\{\{|os\.environ|getenv"
)
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|fixtures?)/|test_[^/]+$|\.test\.|\.spec\.")

FRAPPE_RULES = """Frappe review pack (the repository is a Frappe app):
- A @frappe.whitelist() method must check permissions (frappe.has_permission, doc.check_permission or frappe.only_for) before it reads or writes records for the caller.
- allow_guest=True endpoints must not expose records, and must validate every argument.
- ignore_permissions=True and flags.ignore_permissions need a comment that explains why the bypass is safe.
- frappe.db.sql must take values as parameters. Never build SQL with f-strings, % or + from input.
- Do not call frappe.get_doc, frappe.get_value or frappe.db.get_value in a loop over many records. Fetch in one query.
- A schema change that needs existing data changed must ship a patch in patches.txt.
- User-facing strings in Python and JavaScript must be wrapped in _() or __().
- frappe.db.commit() inside request handlers or document hooks is almost always wrong."""

FRAPPE_LINE_CHECKS = (
	(
		re.compile(r"frappe\.db\.sql\(\s*f[\"']|frappe\.db\.sql\([^)]*[\"']\s*(%|\+|\.format\()"),
		"P1",
		"logic",
		"security",
		"SQL built from strings",
		"This query is built with string formatting. Pass values as parameters to `frappe.db.sql` so input cannot change the SQL.",
	),
	(
		re.compile(r"@frappe\.whitelist\([^)]*allow_guest\s*=\s*True"),
		"P1",
		"logic",
		"security",
		"New guest endpoint",
		"This endpoint is open to anyone without a login. Check that it exposes no records and validates every argument.",
	),
	(
		re.compile(r"ignore_permissions\s*=\s*True"),
		"P2",
		"logic",
		"security",
		"Permission check is bypassed",
		"`ignore_permissions=True` skips the role checks. Confirm the caller is already checked, or remove it.",
	),
	(
		re.compile(r"^\s*frappe\.db\.commit\(\)"),
		"P2",
		"logic",
		"correctness",
		"Explicit commit",
		"Frappe commits at the end of the request. An explicit commit here can save half-finished work when a later step fails.",
	),
)


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


def frappe_findings(files: list[FileDiff]) -> list[Finding]:
	findings = []
	for diff in files:
		if not diff.path.endswith(".py") or TEST_FILE.search(diff.path):
			continue
		for hunk in diff.hunks:
			for line in hunk.lines:
				if line.kind != "+" or not line.new_no:
					continue
				for pattern, severity, comment_type, category, title, body in FRAPPE_LINE_CHECKS:
					if pattern.search(line.text):
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


def _secret_kind(text: str) -> str | None:
	for kind, pattern in SECRET_PATTERNS:
		if pattern.search(text):
			return kind
	match = ASSIGNED_SECRET.search(text)
	if match and not PLACEHOLDER.search(text) and entropy(match.group(2)) > 3.5:
		return "Hard-coded secret"
	return None


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
