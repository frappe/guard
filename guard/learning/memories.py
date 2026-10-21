"""Learned memories: Custom Context of type Pattern."""

from typing import Any

from guard.config.dashboard import active_context
from guard.config.globs import match_path


def for_paths(
	full_name: str | None, team: str | None, paths: list[str], limit: int = 20
) -> list[dict[str, Any]]:
	"""Active memories that apply to the repository and to at least one path."""
	rows = active_context(full_name, team, types=("Pattern",))
	chosen = []
	for row in rows:
		scope = [line.strip() for line in (row.get("path_scope") or "").splitlines() if line.strip()]
		if not scope or any(match_path(path, scope) for path in paths):
			chosen.append(row)
	return chosen[-limit:]
