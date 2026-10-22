"""Project rule files that coding agents use: CLAUDE.md, AGENTS.md and similar."""

import posixpath
import re
from dataclasses import dataclass

from guard.review.run import Lister, Reader

PER_DIRECTORY = ("CLAUDE.md", "AGENTS.md")
ROOT_ONLY = (".cursorrules", ".github/copilot-instructions.md")
MAX_CHARS = 6000
RULE_PREFIX = "project:"
MANDATORY = re.compile(r"\b(always|must|never|do not|don't|mandatory|required)\b", re.I)
TESTS = re.compile(r"\btests?\b", re.I)


@dataclass
class ProjectRule:
	"""A mandatory line of a project rule file. A finding that breaks it is at least P1."""

	path: str
	line: int
	text: str

	@property
	def id(self) -> str:
		return f"{RULE_PREFIX}{self.path}:{self.line}"

	@property
	def is_test_rule(self) -> bool:
		return bool(TESTS.search(self.text))


def collect(paths: list[str], read: Reader, list_dir: Lister | None = None) -> list[tuple[str, str]]:
	"""(path, text) for rule files at the root and on the path of each changed file."""
	directories = {""}
	for path in paths:
		parts = path.split("/")[:-1]
		directories |= {"/".join(parts[: i + 1]) for i in range(len(parts))}
	candidates = [
		posixpath.join(d, name) if d else name for d in sorted(directories) for name in PER_DIRECTORY
	]
	candidates += list(ROOT_ONLY)
	if list_dir:
		try:
			candidates += [
				f".cursor/rules/{name}"
				for name, kind in list_dir(".cursor/rules")
				if kind == "blob" and name.endswith((".md", ".mdc"))
			]
		except Exception:
			pass
	found = []
	for candidate in candidates:
		text = read(candidate)
		if text and text.strip():
			found.append((candidate, text.strip()[:MAX_CHARS]))
	return found


def mandatory_rules(files: list[tuple[str, str]]) -> list[ProjectRule]:
	"""Lines that say always, must, never or similar. Headings and code blocks are skipped."""
	rules = []
	for path, text in files:
		in_code = False
		for number, line in enumerate(text.splitlines(), 1):
			stripped = line.strip()
			if stripped.startswith("```"):
				in_code = not in_code
				continue
			if in_code or stripped.startswith("#") or not MANDATORY.search(stripped):
				continue
			rules.append(ProjectRule(path, number, stripped.lstrip("-* ").strip()))
	return rules
