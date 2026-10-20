"""Glob matching for filters and paths."""

import re
from functools import cache

import pathspec


@cache
def _filter_regex(pattern: str) -> re.Pattern:
	"""`*` one segment, `**` any, `?` one char, `{a,b}` choice. `[`, `]` and `!` are literal."""
	out, i = [], 0
	while i < len(pattern):
		char = pattern[i]
		if pattern.startswith("**", i):
			out.append(".*")
			i += 2
			continue
		if char == "*":
			out.append("[^/]*")
		elif char == "?":
			out.append("[^/]")
		elif char == "{" and "}" in pattern[i:]:
			end = pattern.index("}", i)
			choices = pattern[i + 1 : end].split(",")
			out.append("(?:" + "|".join(re.escape(c) for c in choices) + ")")
			i = end
		else:
			out.append(re.escape(char))
		i += 1
	return re.compile("^" + "".join(out) + "$", re.IGNORECASE)


def match_filter(value: str, patterns: list[str] | None) -> bool:
	"""True when any pattern matches. Used for labels, authors, branches and repositories."""
	return any(_filter_regex(p.strip()).match(value or "") for p in patterns or [] if p and p.strip())


def match_any_filter(values: list[str], patterns: list[str] | None) -> bool:
	return any(match_filter(value, patterns) for value in values)


@cache
def _path_spec(patterns: tuple[str, ...]) -> pathspec.PathSpec:
	return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def match_path(path: str, patterns: list[str] | tuple[str, ...] | str | None) -> bool:
	"""`.gitignore` syntax. A plain directory path also covers everything under it."""
	if not patterns:
		return False
	if isinstance(patterns, str):
		patterns = patterns.splitlines()
	cleaned = tuple(p.strip() for p in patterns if p and p.strip() and not p.strip().startswith("#"))
	if not cleaned:
		return False
	if _path_spec(cleaned).match_file(path):
		return True
	return any(
		_is_plain_dir(p) and (path == p.strip("/") or path.startswith(p.strip("/") + "/")) for p in cleaned
	)


def scope_patterns(directory: str, patterns: list[str] | None) -> list[str]:
	"""Make patterns relative to a config directory into repository-root patterns."""
	if not directory:
		return list(patterns or [])
	prefix = directory.strip("/")
	if not patterns:
		return [f"{prefix}/**"]
	out = []
	for pattern in patterns:
		pattern = pattern.strip()
		negated = pattern.startswith("!")
		body = pattern[1:] if negated else pattern
		body = body.lstrip("/")
		out.append(("!" if negated else "") + f"{prefix}/{body}")
	return out


def _is_plain_dir(pattern: str) -> bool:
	return not any(ch in pattern for ch in "*?[!")
