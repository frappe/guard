"""Read access to Knowledge Base Documents."""

import json
from typing import Any

from guard.utils import db


def documents_for_paths(repository: str, paths: list[str], limit: int = 4) -> list[dict[str, Any]]:
	"""Documents whose source files overlap the paths, best overlap first, plus the reverts page."""
	rows = db.dict_rows(
		"""SELECT name, path, title, content, source_files, section FROM `tabKnowledge Base Document`
		WHERE repository = %s""",
		(repository,),
	)
	wanted = set(paths)
	directories = {p.rsplit("/", 1)[0] for p in paths if "/" in p}
	scored = []
	for row in rows:
		if row["section"] == "reverts":
			continue
		sources = set(_json_list(row["source_files"]))
		score = len(sources & wanted) * 3 + len(
			{s.rsplit("/", 1)[0] for s in sources if "/" in s} & directories
		)
		if score:
			scored.append((score, row))
	scored.sort(key=lambda pair: -pair[0])
	chosen = [row for _, row in scored[:limit]]
	chosen += [row for row in rows if row["section"] == "reverts"][:1]
	return chosen


def _json_list(value: Any) -> list[str]:
	if isinstance(value, list):
		return value
	try:
		data = json.loads(value or "[]")
	except TypeError, json.JSONDecodeError:
		return []
	return [str(v) for v in data] if isinstance(data, list) else []
