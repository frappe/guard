"""Resolve raw edges to entities.

Order for a call, first match wins:
1. `self.x` / `this.x`: a member of the enclosing class or its bases.
2. A definition in the same file.
3. A name the file imports, followed to its definition.
4. The only definition with that name in the repository.
"""

import re
from dataclasses import dataclass

from guard.indexing.parser import ParsedFile, RawEdge
from guard.indexing.symbols import CALLABLE, EntityRow, SymbolTable

SELF_RECEIVERS = {"self", "this", "cls", "$this", "@", "super()", "super"}
TYPE_KINDS = ("class", "interface", "type")


@dataclass
class EdgeRow:
	source_entity: str
	target_entity: str | None
	target_name: str
	target_text: str
	kind: str
	path: str
	line: int


@dataclass
class ImportTarget:
	file: EntityRow | None
	name: str | None  # imported symbol, None for a whole module


class Resolver:
	def __init__(self, table: SymbolTable):
		self.table = table

	def resolve_file(self, parsed: ParsedFile, names: dict[str, str]) -> list[EdgeRow]:
		imports = self._import_map(parsed)
		rows = []
		for edge in parsed.edges:
			source = names.get(edge.source_key)
			if not source:
				continue
			target = self._target(parsed, edge, source, imports, names)
			rows.append(
				EdgeRow(
					source,
					target,
					edge.target_name[:140],
					edge.target_text[:500],
					edge.kind,
					parsed.path,
					edge.line,
				)
			)
		return rows

	def resolve_name(self, target_name: str, kind: str, language: str | None = None) -> str | None:
		"""Resolve an edge with no file context. Used when targets change."""
		if kind == "references" or (kind == "calls" and "." in target_name):
			return _first(self.table.by_qualified.get(target_name))
		if kind == "inherits":
			row = self.table.unique(target_name, TYPE_KINDS, language)
		else:
			row = self.table.unique(target_name, language=language)
		return row.name if row else None

	# ------------------------------------------------------------ targets

	def _target(self, parsed, edge: RawEdge, source: str, imports, names) -> str | None:
		if edge.kind == "contains":
			return names.get(edge.target_key or "")
		if edge.kind == "imports":
			ref = next(
				(r for r in parsed.imports if r.module == edge.target_name and r.line == edge.line), None
			)
			row = self.table.module_file(
				edge.target_name, parsed.path, parsed.language, bool(ref and ref.relative)
			)
			return row.name if row else None
		if edge.kind == "references" or (
			edge.kind == "calls" and edge.target_name == edge.target_text and "." in edge.target_name
		):
			return _first(self.table.by_qualified.get(edge.target_name))
		if edge.kind == "inherits":
			row = self._type(parsed, edge.target_name, imports)
			return row.name if row else None
		row = self._call(parsed, edge, source, imports)
		return row.name if row else None

	def _call(self, parsed: ParsedFile, edge: RawEdge, source: str, imports) -> EntityRow | None:
		name = edge.target_name
		receiver = _receiver(edge.target_text)
		if receiver in SELF_RECEIVERS:
			owner = self.table.enclosing_class(source)
			if owner and (row := self.table.member(owner.name, name)):
				return row
		if receiver:
			if row := self._via_receiver(parsed, receiver, name, imports):
				return row
		elif row := self.table.in_file(parsed.path, name):
			return row
		elif row := self._imported(imports.get(name)):
			return row
		return self.table.unique(name, language=parsed.language)

	def _via_receiver(self, parsed: ParsedFile, receiver: str, name: str, imports) -> EntityRow | None:
		head = re.sub(r"\(.*\)$", "", receiver).split(".")[0]
		target = imports.get(head)
		if target and target.file and target.name is None:
			return self.table.in_file(target.file.path, name)
		owner = self._imported(target) if target else None
		owner = (
			owner or self.table.in_file(parsed.path, head, TYPE_KINDS) or self.table.unique(head, TYPE_KINDS)
		)
		if owner and owner.kind in TYPE_KINDS:
			return self.table.member(owner.name, name)
		return None

	def _type(self, parsed: ParsedFile, name: str, imports) -> EntityRow | None:
		return (
			self.table.in_file(parsed.path, name, TYPE_KINDS)
			or self._imported(imports.get(name), TYPE_KINDS)
			or self.table.unique(name, TYPE_KINDS, parsed.language)
		)

	def _imported(self, target: ImportTarget | None, kinds: tuple[str, ...] = CALLABLE) -> EntityRow | None:
		if not target or not target.file or not target.name or target.name == "default":
			return None
		row = self.table.in_file(target.file.path, target.name, kinds)
		if row:
			return row
		qualified = f"{target.file.qualified_name}.{target.name}"
		return next((r for r in self.table.by_qualified.get(qualified, []) if r.kind in kinds), None)

	def _import_map(self, parsed: ParsedFile) -> dict[str, ImportTarget]:
		mapping: dict[str, ImportTarget] = {}
		for ref in parsed.imports:
			file = self.table.module_file(ref.module, parsed.path, parsed.language, ref.relative)
			if ref.alias:
				mapping[ref.alias] = ImportTarget(file, None)
			for local, imported in ref.names.items():
				submodule = self.table.module_file(
					f"{ref.module}.{imported}", parsed.path, parsed.language, ref.relative
				)
				if parsed.language == "python" and submodule:
					mapping[local] = ImportTarget(submodule, None)
				else:
					mapping[local] = ImportTarget(file, imported)
		return mapping


def _receiver(text: str) -> str:
	"""`self.repo.save` gives `self.repo`. A bare name gives an empty string."""
	parts = re.split(r"\.|->|::", text)
	return ".".join(parts[:-1]).strip() if len(parts) > 1 else ""


def _first(rows: list[EntityRow] | None) -> str | None:
	return rows[0].name if rows else None
