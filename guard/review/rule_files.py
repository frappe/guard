"""Project rule files that coding agents use: CLAUDE.md, AGENTS.md and similar."""

import posixpath

from guard.review.run import Lister, Reader

PER_DIRECTORY = ("CLAUDE.md", "AGENTS.md")
ROOT_ONLY = (".cursorrules", ".github/copilot-instructions.md")
MAX_CHARS = 6000


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
