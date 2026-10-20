"""Remove secrets from text before it reaches logs or error fields."""

import re

PATTERNS = (
	re.compile(r"(gh[pousr]_[A-Za-z0-9]{20,})"),
	re.compile(r"(github_pat_[A-Za-z0-9_]{20,})"),
	re.compile(r"(glpat-[A-Za-z0-9_\-]{16,})"),
	re.compile(r"(sk-[A-Za-z0-9_\-]{20,})"),
	re.compile(r"(xox[baprs]-[A-Za-z0-9\-]{10,})"),
	re.compile(r"(AKIA[0-9A-Z]{16})"),
	re.compile(r"(?i)(authorization:\s*\S+\s+)\S+"),
	re.compile(r"(?i)(https?://)[^/\s:@]+:[^/\s@]+@"),
	re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
)


def redact(text: str) -> str:
	for pattern in PATTERNS:
		if pattern.groups >= 1 and pattern.pattern.startswith("(?i)("):
			text = pattern.sub(lambda m: m.group(1) + "[redacted]", text)
		else:
			text = pattern.sub("[redacted]", text)
	return text
