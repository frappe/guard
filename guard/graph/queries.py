"""Queries over the graph of one Repository Index."""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from guard.indexing.symbols import family
from guard.indexing.vectors import VectorStore
from guard.llm.embeddings import Embedder, tokens
from guard.utils import db

ENTITY_FIELDS = (
	"name, kind, entity_name, qualified_name, path, parent_entity, start_line, end_line, "
	"signature, language, is_entry_point"
)
CALLER_LIMIT = 50
RRF_K = 60


@dataclass
class Entity:
	name: str
	kind: str
	entity_name: str
	qualified_name: str
	path: str
	parent_entity: str | None
	start_line: int
	end_line: int
	signature: str
	language: str | None
	is_entry_point: bool = False

	@property
	def label(self) -> str:
		return f"{self.qualified_name} ({self.path}:{self.start_line})"


@dataclass
class Link:
	"""One edge seen from one end."""

	entity: Entity | None
	name: str
	text: str
	path: str
	line: int
	resolved: bool


@dataclass
class ChunkHit:
	name: str
	path: str
	start_line: int
	end_line: int
	content: str
	score: float
	entity: str | None = None


@dataclass
class Impact:
	files: dict[str, list[Link]] = field(default_factory=dict)

	@property
	def caller_count(self) -> int:
		return sum(len(links) for links in self.files.values())


class CodeGraph:
	def __init__(self, index: str):
		self.index = index
		self.repository = str(db.value("Repository Index", index, "repository") or "")

	# ------------------------------------------------------------ entities

	def entity(self, name: str) -> Entity | None:
		rows = self._entities("name = %s", (name,))
		return rows[0] if rows else None

	def definitions(self, name: str, kind: str | None = None, limit: int = 20) -> list[Entity]:
		"""Entities with this name, qualified name, or qualified-name suffix."""
		condition = "(entity_name = %s OR qualified_name = %s OR qualified_name LIKE %s) AND kind != 'file'"
		values: tuple = (name, name, f"%.{_escape_like(name)}")
		if kind:
			condition += " AND kind = %s"
			values += (kind,)
		return self._entities(condition, values, limit=limit, order="kind = 'file', LENGTH(qualified_name)")

	def outline(self, path: str) -> list[Entity]:
		return self._entities("path = %s", (path,), order="start_line, kind = 'file' DESC", limit=2000)

	def file(self, path: str) -> Entity | None:
		rows = self._entities("path = %s AND kind = 'file'", (path,), limit=1)
		return rows[0] if rows else None

	def entities_in_range(self, path: str, start: int, end: int) -> list[Entity]:
		"""The innermost callable entities overlapping a line range."""
		rows = self._entities(
			"path = %s AND kind IN ('function', 'method', 'class', 'doctype') AND start_line <= %s AND end_line >= %s",
			(path, end, start),
			order="end_line - start_line",
		)
		parents = {row.parent_entity for row in rows}
		return [row for row in rows if row.name not in parents] or rows

	# --------------------------------------------------------------- edges

	def callers(self, entity: Entity, limit: int = CALLER_LIMIT) -> list[Link]:
		"""Who calls the entity. Name-only edges come back with resolved=False."""
		rows = db.dict_rows(
			"""SELECT source_entity, target_entity, target_name, target_text, path, line FROM `tabCode Edge`
			WHERE `index` = %s AND kind IN ('calls', 'references')
				AND (target_entity = %s OR ((target_entity IS NULL OR target_entity = '') AND target_name = %s))
			ORDER BY target_entity IS NULL, path, line LIMIT %s""",
			(self.index, entity.name, entity.entity_name, limit * 2),
		)
		sources = self._by_names({row["source_entity"] for row in rows})
		links = []
		for row in rows:
			source = sources.get(row["source_entity"])
			resolved = bool(row["target_entity"])
			if not resolved and source and family(source.language) != family(entity.language):
				continue
			links.append(
				Link(
					source,
					row["target_name"],
					row["target_text"] or "",
					row["path"],
					row["line"] or 0,
					resolved,
				)
			)
		return links[:limit]

	def callees(self, entity: Entity, limit: int = 100) -> list[Link]:
		rows = db.dict_rows(
			"""SELECT target_entity, target_name, target_text, path, line FROM `tabCode Edge`
			WHERE `index` = %s AND source_entity = %s AND kind = 'calls' ORDER BY line LIMIT %s""",
			(self.index, entity.name, limit),
		)
		targets = self._by_names({row["target_entity"] for row in rows if row["target_entity"]})
		return [
			Link(
				targets.get(row["target_entity"]),
				row["target_name"],
				row["target_text"] or "",
				row["path"],
				row["line"] or 0,
				bool(row["target_entity"]),
			)
			for row in rows
		]

	def references(self, entity: Entity, limit: int = 100) -> list[Link]:
		"""Imports and string references that point to the entity or to its file."""
		targets = [entity.name]
		if entity.kind != "file" and (file := self.file(entity.path)):
			targets.append(file.name)
		rows = db.dict_rows(
			"""SELECT source_entity, target_name, target_text, path, line, kind FROM `tabCode Edge`
			WHERE `index` = %s AND kind IN ('imports', 'references') AND target_entity IN %s LIMIT %s""",
			(self.index, tuple(targets), limit),
		)
		sources = self._by_names({row["source_entity"] for row in rows})
		return [
			Link(
				sources.get(r["source_entity"]),
				r["target_name"],
				r["target_text"] or "",
				r["path"],
				r["line"] or 0,
				True,
			)
			for r in rows
		]

	def impact(self, entities: list[Entity], depth: int = 2) -> Impact:
		"""Transitive callers, grouped by file."""
		result = Impact()
		seen = {e.name for e in entities}
		frontier = list(entities)
		for _ in range(depth):
			next_frontier: list[Entity] = []
			for entity in frontier:
				for link in self.callers(entity):
					result.files.setdefault(link.path, []).append(link)
					if link.entity and link.resolved and link.entity.name not in seen:
						seen.add(link.entity.name)
						next_frontier.append(link.entity)
			frontier = next_frontier
		return result

	def siblings(self, entity: Entity, limit: int = 8) -> list[Entity]:
		"""Entities of the same kind with shared name tokens, near the entity."""
		directory = entity.path.rsplit("/", 1)[0] if "/" in entity.path else ""
		candidates = self._entities(
			"kind = %s AND name != %s AND path LIKE %s",
			(entity.kind, entity.name, f"{_escape_like(directory)}%"),
			limit=500,
		)
		mine = set(tokens(entity.entity_name))
		scored = []
		for candidate in candidates:
			theirs = set(tokens(candidate.entity_name))
			if not mine or not theirs:
				continue
			score = len(mine & theirs) / len(mine | theirs)
			if score >= 0.34:
				scored.append((score + (0.1 if candidate.path == entity.path else 0), candidate))
		scored.sort(key=lambda pair: -pair[0])
		return [candidate for _, candidate in scored[:limit]]

	def dependencies(self, path: str) -> dict[str, list[str]]:
		file = self.file(path)
		if not file:
			return {"imports": [], "imported_by": []}
		out = db.rows(
			"""SELECT DISTINCT t.path FROM `tabCode Edge` g JOIN `tabCode Entity` t ON t.name = g.target_entity
			WHERE g.`index` = %s AND g.kind = 'imports' AND g.source_entity = %s""",
			(self.index, file.name),
		)
		into = db.rows(
			"""SELECT DISTINCT path FROM `tabCode Edge` WHERE `index` = %s AND kind = 'imports' AND target_entity = %s""",
			(self.index, file.name),
		)
		return {"imports": [r[0] for r in out], "imported_by": [r[0] for r in into]}

	# -------------------------------------------------------------- search

	def search(self, query: str, k: int = 10) -> list[ChunkHit]:
		"""Hybrid search: FULLTEXT rank and vector rank, merged with reciprocal rank fusion."""
		symbols = self._symbol_hits(query, k * 2)
		lexical = self._lexical(query, k * 3)
		vector = self.similar(query, k * 3)
		scores: dict[str, float] = defaultdict(float)
		hits: dict[str, ChunkHit] = {}
		for ranked in (symbols, symbols, lexical, vector):
			for rank, hit in enumerate(ranked):
				scores[hit.name] += 1 / (RRF_K + rank + 1)
				hits.setdefault(hit.name, hit)
		order = sorted(scores, key=lambda name: -scores[name])[:k]
		return [ChunkHit(**{**hits[name].__dict__, "score": round(scores[name], 5)}) for name in order]

	def similar(self, text: str, k: int = 10) -> list[ChunkHit]:
		vector = Embedder().embed_one(text)
		pairs = VectorStore().nearest("chunk", vector, k * 2, [self.repository] if self.repository else None)
		chunks = self._chunks([ref for ref, _ in pairs])
		distance = dict(pairs)
		hits = [
			ChunkHit(**{**chunks[ref], "score": round(1 - distance[ref], 5)})
			for ref, _ in pairs
			if ref in chunks
		]
		return hits[:k]

	def _symbol_hits(self, query: str, k: int) -> list[ChunkHit]:
		"""Chunks of entities whose names share tokens with the query."""
		words = [w for w in dict.fromkeys(tokens(query)) if len(w) > 2][:6]
		if not words:
			return []
		condition = " OR ".join(["entity_name LIKE %s"] * len(words))
		candidates = self._entities(
			f"kind IN ('function', 'method', 'class', 'interface', 'type', 'doctype') AND ({condition})",
			tuple(f"%{_escape_like(w)}%" for w in words),
			limit=300,
		)
		wanted = set(words)
		scored = sorted(
			(
				(len(wanted & set(tokens(e.entity_name))) / len(wanted | set(tokens(e.entity_name))), e)
				for e in candidates
			),
			key=lambda pair: -pair[0],
		)
		entities = [e.name for score, e in scored if score > 0][:k]
		if not entities:
			return []
		rows = db.dict_rows(
			"SELECT name, path, start_line, end_line, content, entity FROM `tabCode Chunk` WHERE `index` = %s AND entity IN %s",
			(self.index, tuple(entities)),
		)
		order = {name: i for i, name in enumerate(entities)}
		rows.sort(key=lambda r: (order.get(r["entity"], 999), r["start_line"]))
		return [
			ChunkHit(r["name"], r["path"], r["start_line"], r["end_line"], r["content"], 0.0, r["entity"])
			for r in rows
		]

	def _lexical(self, query: str, k: int) -> list[ChunkHit]:
		words = sorted(
			{w for w in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)}
			| set(t for t in tokens(query) if len(t) > 2)
		)
		if not words:
			return []
		rows = db.dict_rows(
			"""SELECT name, path, start_line, end_line, content, entity,
				MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE) AS score
			FROM `tabCode Chunk` WHERE `index` = %s AND MATCH(content) AGAINST (%s IN NATURAL LANGUAGE MODE)
			ORDER BY score DESC LIMIT %s""",
			(" ".join(words), self.index, " ".join(words), k),
		)
		return [
			ChunkHit(
				r["name"],
				r["path"],
				r["start_line"],
				r["end_line"],
				r["content"],
				float(r["score"]),
				r["entity"],
			)
			for r in rows
		]

	def _chunks(self, names: list[str]) -> dict[str, dict[str, Any]]:
		if not names:
			return {}
		rows = db.dict_rows(
			"""SELECT name, path, start_line, end_line, content, entity FROM `tabCode Chunk`
			WHERE name IN %s AND `index` = %s""",
			(tuple(names), self.index),
		)
		return {r["name"]: {**r, "score": 0.0} for r in rows}

	# ------------------------------------------------------------- helpers

	def _entities(
		self, condition: str, values: tuple, limit: int = 100, order: str = "path, start_line"
	) -> list[Entity]:
		rows = db.dict_rows(
			f"SELECT {ENTITY_FIELDS} FROM `tabCode Entity` WHERE `index` = %s AND {condition} ORDER BY {order} LIMIT {int(limit)}",
			(self.index, *values),
		)
		return [_entity(row) for row in rows]

	def _by_names(self, names: set[str]) -> dict[str, Entity]:
		names = {n for n in names if n}
		if not names:
			return {}
		return {e.name: e for e in self._entities("name IN %s", (tuple(names),), limit=len(names))}


def _entity(row: dict[str, Any]) -> Entity:
	return Entity(
		name=row["name"],
		kind=row["kind"] or "",
		entity_name=row["entity_name"] or "",
		qualified_name=row["qualified_name"] or "",
		path=row["path"] or "",
		parent_entity=row["parent_entity"],
		start_line=row["start_line"] or 0,
		end_line=row["end_line"] or 0,
		signature=row["signature"] or "",
		language=row["language"],
		is_entry_point=bool(row["is_entry_point"]),
	)


def _escape_like(text: str) -> str:
	return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
