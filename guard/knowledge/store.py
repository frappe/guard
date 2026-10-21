"""Read access to Knowledge Base Documents."""

import json
import re
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


def search(repository: str, query: str, limit: int = 10) -> list[dict[str, Any]]:
	words = [w for w in re.findall(r"\w{3,}", query.lower())][:8]
	if not words:
		return []
	rows = db.dict_rows(
		"SELECT name, path, title, content FROM `tabKnowledge Base Document` WHERE repository = %s",
		(repository,),
	)
	results = []
	for row in rows:
		text = f"{row['title'] or ''}\n{row['content'] or ''}".lower()
		hits = sum(text.count(word) for word in words)
		if hits:
			position = min((text.find(w) for w in words if w in text), default=0)
			results.append(
				{
					"path": row["path"],
					"title": row["title"],
					"hits": hits,
					"snippet": (row["content"] or "")[max(position - 200, 0) : position + 200],
				}
			)
	results.sort(key=lambda r: -r["hits"])
	return results[:limit]


def read(repository: str, path: str) -> dict[str, Any] | None:
	rows = db.dict_rows(
		"SELECT name, path, title, content, version_id FROM `tabKnowledge Base Document` WHERE repository = %s AND path = %s",
		(repository, path),
	)
	return rows[0] if rows else None


def list_paths(repository: str) -> list[str]:
	return [
		r[0]
		for r in db.rows(
			"SELECT path FROM `tabKnowledge Base Document` WHERE repository = %s ORDER BY path", (repository,)
		)
	]


def _json_list(value: Any) -> list[str]:
	if isinstance(value, list):
		return value
	try:
		data = json.loads(value or "[]")
	except TypeError, json.JSONDecodeError:
		return []
	return [str(v) for v in data] if isinstance(data, list) else []
