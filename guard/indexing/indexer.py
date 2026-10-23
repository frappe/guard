"""Build and update the graph, chunks and vectors of one Repository Index."""

import hashlib
from collections import Counter
from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, cast

import frappe
from frappe.utils import now_datetime

from guard.git.repository import store_for
from guard.git.store import GitStore, TreeEntry
from guard.indexing import frappe_plugin, languages
from guard.indexing.chunker import Chunk, chunk_file
from guard.indexing.parser import PARSER_VERSION, ParsedFile, parse_file
from guard.indexing.resolver import EdgeRow, Resolver
from guard.indexing.selection import FileSelector, is_text
from guard.indexing.symbols import EntityRow, SymbolTable
from guard.indexing.vectors import VectorRow, VectorStore
from guard.llm.embeddings import Embedder
from guard.utils import cache, db

if TYPE_CHECKING:
	from guard.guard.doctype.repository_index.repository_index import RepositoryIndex

BATCH = 300
LOCK_TTL = 7200  # the timeout of the index job
FAILED_FOR = 3600  # seconds a review skips an index that failed
# A run sets these. When a new run finds one, the run before it died and left the rows half done.
RUNNING = ("cloning", "processing")
# The qualified name of code has the module path in it. These kinds have none, so two files
# can hold the same one: three services can each serve GET /health. Their names use the path.
PATH_IN_NAME = ("file", "route", "doctype", "field")
ENTITY_COLUMNS = (
	"name",
	"creation",
	"modified",
	"owner",
	"modified_by",
	"index",
	"repository",
	"path",
	"kind",
	"entity_name",
	"qualified_name",
	"parent_entity",
	"language",
	"start_line",
	"end_line",
	"signature",
	"docstring",
	"is_exported",
	"is_entry_point",
	"content_hash",
)
EDGE_COLUMNS = (
	"name",
	"creation",
	"modified",
	"owner",
	"modified_by",
	"index",
	"repository",
	"source_entity",
	"target_entity",
	"target_name",
	"target_text",
	"kind",
	"path",
	"line",
)
CHUNK_COLUMNS = (
	"name",
	"creation",
	"modified",
	"owner",
	"modified_by",
	"index",
	"repository",
	"path",
	"entity",
	"start_line",
	"end_line",
	"language",
	"content",
	"content_hash",
)


def short_hash(*parts: object) -> str:
	return hashlib.blake2b("|".join(str(p) for p in parts).encode(), digest_size=10).hexdigest()


class IndexBusy(Exception):
	"""Another job is indexing the same branch now."""


class IndexFailed(Exception):
	"""The index of the branch failed, now or in the last hour. A review goes on without it."""


def index_lock(index: str) -> AbstractContextManager[bool]:
	"""Held while a run writes the index, so two runs never write the same rows."""
	return cache.exclusive(f"guard:index:{index}", LOCK_TTL)


def recent_failure(index: str) -> str:
	"""The error of a run that failed in the last hour, else an empty string."""
	return str(cache.get(_failed_key(index)) or "")


def _failed_key(index: str) -> str:
	return f"guard:index-failed:{index}"


class Indexer:
	def __init__(self, index: str, full: bool = False):
		self.doc = cast("RepositoryIndex", frappe.get_doc("Repository Index", index))
		self.index = str(self.doc.name)
		self.repository = self.doc.repository
		self.full = full
		self.previous: str | None = None  # the sha of the last complete run
		self.now = now_datetime()

	def run(self) -> None:
		with index_lock(self.index) as acquired:
			if not acquired:
				raise IndexBusy(self.index)
			self.doc.reload()
			try:
				self._run()
			except Exception as error:
				db.rollback()
				# Without a sha, the next run indexes every file again.
				self._set(status="failed", sha=None, error=str(error)[:5000])
				frappe.db.set_value("Code Repository", self.repository, "index_status", "Failed")
				db.commit()
				# Reviews skip the index for an hour, rather than fail the same way one by one.
				cache.set(_failed_key(self.index), str(error)[:500], FAILED_FOR)
				raise
			cache.delete(_failed_key(self.index))

	def _run(self) -> None:
		# Read it before the status changes below.
		self.previous = None if self.doc.status in RUNNING else self.doc.sha
		self._set(status="cloning", started_at=now_datetime(), error=None)
		store = store_for(self.repository)
		store.fetch_branches(self.doc.branch)
		sha = store.rev_parse(f"refs/heads/{self.doc.branch}")
		self._set(status="processing")
		selector = FileSelector(store, sha)
		selected = selector.select(store.ls_tree(sha))
		changed, removed = self._changes(store, sha, selected)
		self._set(num_files=len(selected), files_processed=0)
		removed_names = self._delete_paths(changed | removed)
		table = SymbolTable.load(self.index)
		parsed = self._parse(
			store, [selected[p] for p in sorted(changed)], table, frappe_plugin.is_frappe_app(set(selected))
		)
		new_names = self._resolve(parsed, table, removed_names)
		self._controller_edges(set(selected), changed, table)
		self._chunks(parsed)
		self._finish(store, sha, selected, removed_names, new_names)

	# ----------------------------------------------------------- changes

	def _changes(
		self, store: GitStore, sha: str, selected: dict[str, TreeEntry]
	) -> tuple[set[str], set[str]]:
		previous = self.previous
		incremental = (
			not self.full
			and previous
			and self.doc.parser_version == PARSER_VERSION
			and store.has_commit(previous)
		)
		if not incremental:
			existing = {
				row[0]
				for row in db.rows(
					"SELECT DISTINCT path FROM `tabCode Entity` WHERE `index` = %s", (self.index,)
				)
			}
			return set(selected), existing - set(selected)
		changed, removed = set(), set()
		for status, path, old_path in store.name_status(str(previous), sha):
			if status == "D":
				removed.add(path)
			elif path in selected:
				changed.add(path)
			else:
				removed.add(path)
			if old_path:
				removed.add(old_path)
		return changed, removed

	def _delete_paths(self, paths: set[str]) -> set[str]:
		names: set[str] = set()
		for batch in _batches(sorted(paths), 500):
			values = (self.index, tuple(batch))
			names |= {
				r[0]
				for r in db.rows(
					"SELECT name FROM `tabCode Entity` WHERE `index` = %s AND path IN %s", values
				)
			}
			chunks = [
				r[0]
				for r in db.rows("SELECT name FROM `tabCode Chunk` WHERE `index` = %s AND path IN %s", values)
			]
			VectorStore().delete_refs("chunk", chunks)
			filters = {"index": self.index, "path": ["in", batch]}
			for doctype in ("Code Entity", "Code Edge", "Code Chunk"):
				frappe.db.delete(doctype, filters)
		return names

	# ------------------------------------------------------------- parse

	def _parse(
		self, store: GitStore, entries: list[TreeEntry], table: SymbolTable, frappe_app: bool
	) -> list[tuple[ParsedFile, dict[str, str], str]]:
		results = []
		for count, batch in enumerate(_batches(entries, BATCH), 1):
			blobs = store.read_blobs([entry.blob for entry in batch])
			rows = []
			for entry in batch:
				data = blobs.get(entry.blob)
				if data is None or not is_text(data):
					continue
				source = data.decode("utf-8", "replace")
				parsed = parse_file(entry.path, source)
				if frappe_app:
					frappe_plugin.augment(parsed, source)
				names = self._names(parsed, table)
				rows += self._entity_rows(parsed, names, entry.blob)
				for row in _entity_rows_for_table(parsed, names):
					table.add(row)
				results.append((parsed, names, source))
			db.insert_many("tabCode Entity", ENTITY_COLUMNS, rows)
			self._set(files_processed=min(count * BATCH, len(entries)))
			db.commit()
		return results

	def _names(self, parsed: ParsedFile, table: SymbolTable) -> dict[str, str]:
		names: dict[str, str] = {}
		seen: Counter[tuple[str, str]] = Counter()
		for entity in parsed.entities:
			occurrence = seen[(entity.kind, entity.qualified_name)]
			seen[(entity.kind, entity.qualified_name)] += 1
			name = short_hash(
				self.index,
				parsed.path if entity.kind in PATH_IN_NAME else "",
				entity.kind,
				entity.qualified_name,
				occurrence,
			)
			if name in table.rows:
				# Another file has the same entity, for example `x.js` and `x.ts`. One clash must
				# not fail the whole index, so give this one a name with its path.
				frappe.logger("guard.indexer").warning(
					f"{self.index}: {entity.kind} {entity.qualified_name} in {parsed.path} "
					f"has the name of one in {table.rows[name].path}"
				)
				name = short_hash(name, parsed.path)
			names[entity.key] = name
		return names

	def _entity_rows(self, parsed: ParsedFile, names: dict[str, str], blob: str) -> list[tuple]:
		rows = []
		for entity in parsed.entities:
			rows.append(
				(
					names[entity.key],
					self.now,
					self.now,
					"Administrator",
					"Administrator",
					self.index,
					self.repository,
					parsed.path,
					entity.kind,
					entity.name[:255],
					entity.qualified_name,
					names.get(entity.parent_key or ""),
					parsed.language,
					entity.start_line,
					entity.end_line,
					entity.signature,
					entity.docstring,
					int(entity.is_exported),
					int(entity.is_entry_point),
					blob if entity.kind == "file" else None,
				)
			)
		return rows

	# ----------------------------------------------------------- resolve

	def _resolve(
		self, parsed: list[tuple[ParsedFile, dict[str, str], str]], table: SymbolTable, removed: set[str]
	) -> set[str]:
		resolver = Resolver(table)
		edges: list[EdgeRow] = []
		new_names: set[str] = set()
		for file, names, _ in parsed:
			edges += resolver.resolve_file(file, names)
			new_names |= set(names.values())
		self._insert_edges(edges)
		self._repair(resolver, removed - new_names, new_names - removed, table)
		return new_names

	def _repair(self, resolver: Resolver, dangling: set[str], fresh: set[str], table: SymbolTable) -> None:
		"""Fix edges of unchanged files whose targets changed."""
		candidates: list[tuple] = []
		for batch in _batches(sorted(dangling), 500):
			candidates += db.rows(
				"""SELECT g.name, g.target_name, g.kind, s.language FROM `tabCode Edge` g
				LEFT JOIN `tabCode Entity` s ON s.name = g.source_entity
				WHERE g.`index` = %s AND g.target_entity IN %s""",
				(self.index, tuple(batch)),
			)
		fresh_names = sorted(
			{table.rows[n].entity_name for n in fresh if n in table.rows}
			| {table.rows[n].qualified_name for n in fresh if n in table.rows}
		)
		for batch in _batches(fresh_names, 500):
			candidates += db.rows(
				"""SELECT name, target_name, kind FROM `tabCode Edge`
				WHERE `index` = %s AND (target_entity IS NULL OR target_entity = '') AND target_name IN %s""",
				(self.index, tuple(batch)),
			)
		for name, target_name, kind in candidates:
			db.execute(
				"UPDATE `tabCode Edge` SET target_entity = %s WHERE name = %s",
				(resolver.resolve_name(target_name, kind), name),
			)

	def _controller_edges(self, paths: set[str], changed: set[str], table: SymbolTable) -> None:
		edges = []
		for controller, json_path in frappe_plugin.controller_edges(paths):
			if controller not in changed and json_path not in changed:
				continue
			source = table.files.get(controller)
			doctype = next((r for r in table.by_path.get(json_path, []) if r.kind == "doctype"), None)
			if source and doctype:
				edges.append(
					EdgeRow(
						source.name,
						doctype.name,
						doctype.qualified_name,
						doctype.entity_name,
						"references",
						controller,
						1,
					)
				)
		self._insert_edges(edges)

	def _insert_edges(self, edges: list[EdgeRow]) -> None:
		rows = [
			(
				short_hash(self.index, e.source_entity, e.kind, e.target_name, e.line, i),
				self.now,
				self.now,
				"Administrator",
				"Administrator",
				self.index,
				self.repository,
				e.source_entity,
				e.target_entity,
				e.target_name,
				e.target_text,
				e.kind,
				e.path,
				e.line,
			)
			for i, e in enumerate(edges)
		]
		db.insert_many("tabCode Edge", EDGE_COLUMNS, rows, replace=True)

	# ------------------------------------------------------------ chunks

	def _chunks(self, parsed: list[tuple[ParsedFile, dict[str, str], str]]) -> None:
		embedder = Embedder()
		store = VectorStore()
		for batch in _batches(parsed, 100):
			chunks: list[tuple[Chunk, str | None, str]] = []
			for file, names, source in batch:
				for chunk in chunk_file(file, source):
					chunks.append((chunk, names.get(chunk.entity_key or ""), file.language or ""))
			rows = []
			for chunk, entity, language in chunks:
				digest = hashlib.blake2b(chunk.content.encode(), digest_size=16).hexdigest()
				name = short_hash(self.index, chunk.path, chunk.start_line, digest)
				rows.append(
					(
						name,
						self.now,
						self.now,
						"Administrator",
						"Administrator",
						self.index,
						self.repository,
						chunk.path,
						entity,
						chunk.start_line,
						chunk.end_line,
						language,
						chunk.content,
						digest,
					)
				)
			db.insert_many("tabCode Chunk", CHUNK_COLUMNS, rows, replace=True)
			vectors = embedder.embed([row[12] for row in rows])
			store.upsert(
				[
					VectorRow(row[0], "chunk", row[0], self.repository, vector)
					for row, vector in zip(rows, vectors, strict=True)
				]
			)
			db.commit()

	# ------------------------------------------------------------ finish

	def _finish(
		self, store: GitStore, sha: str, selected: dict[str, TreeEntry], removed: set[str], new: set[str]
	) -> None:
		counts = {
			doctype: db.scalar(f"SELECT COUNT(*) FROM `tab{doctype}` WHERE `index` = %s", (self.index,))
			for doctype in ("Code Entity", "Code Edge", "Code Chunk")
		}
		self._set(
			status="completed",
			sha=sha,
			parser_version=PARSER_VERSION,
			completed_at=now_datetime(),
			entity_count=counts["Code Entity"],
			edge_count=counts["Code Edge"],
			chunk_count=counts["Code Chunk"],
			files_processed=len(selected),
		)
		repository = frappe.get_doc("Code Repository", self.repository)
		if repository.get("default_branch") == self.doc.branch:
			repository.db_set(
				{
					"index_status": "Indexed",
					"default_index": self.index,
					"languages": frappe.as_json(_language_bytes(selected.values())),
					"size_kb": store.size_kb(),
				}
			)
		db.commit()
		frappe.publish_realtime(
			"guard_index_updated",
			{"repository": self.repository, "index": self.index},
			doctype="Code Repository",
			docname=self.repository,
		)

	def _set(self, **values) -> None:
		self.doc.db_set(values, update_modified=False)


def _entity_rows_for_table(parsed: ParsedFile, names: dict[str, str]) -> list[EntityRow]:
	return [
		EntityRow(
			name=names[e.key],
			kind=e.kind,
			entity_name=e.name,
			qualified_name=e.qualified_name,
			path=parsed.path,
			parent_entity=names.get(e.parent_key or ""),
			start_line=e.start_line,
			end_line=e.end_line,
			language=parsed.language,
			signature=e.signature,
		)
		for e in parsed.entities
	]


def _language_bytes(entries: Iterable[TreeEntry]) -> dict[str, int]:
	totals: Counter[str] = Counter()
	for entry in entries:
		language = languages.detect(entry.path)
		if language:
			totals[language] += entry.size
	return dict(totals.most_common())


def _batches(items: list, size: int):
	for start in range(0, len(items), size):
		yield items[start : start + size]
