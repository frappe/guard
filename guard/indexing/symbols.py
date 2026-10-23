"""In-memory symbol table over the entities of one index."""

import posixpath
from collections import defaultdict
from dataclasses import dataclass

from guard.utils import db

CALLABLE = ("function", "method", "class")
FAMILIES = {"javascript": "js", "typescript": "js", "tsx": "js", "vue": "js", "c": "c", "cpp": "c"}


def family(language: str | None) -> str:
	return FAMILIES.get(language or "", language or "")


@dataclass
class EntityRow:
	name: str
	kind: str
	entity_name: str
	qualified_name: str
	path: str
	parent_entity: str | None
	start_line: int
	end_line: int
	language: str | None = None
	signature: str = ""


class SymbolTable:
	def __init__(self, rows: list[EntityRow]):
		self.rows = {row.name: row for row in rows}
		self.by_qualified: dict[str, list[EntityRow]] = defaultdict(list)
		self.by_name: dict[str, list[EntityRow]] = defaultdict(list)
		self.by_path: dict[str, list[EntityRow]] = defaultdict(list)
		self.files: dict[str, EntityRow] = {}
		self.modules: dict[str, EntityRow] = {}
		# File name without extension -> path -> file row, in the order of `files`.
		self.stems: dict[str, dict[str, EntityRow]] = defaultdict(dict)
		self.children: dict[str, dict[str, EntityRow]] = defaultdict(dict)
		self.bases: dict[str, list[str]] = defaultdict(list)
		for row in rows:
			self.add(row)

	@classmethod
	def load(cls, index: str) -> "SymbolTable":
		result = db.dict_rows(
			"""SELECT name, kind, entity_name, qualified_name, path, parent_entity,
				start_line, end_line, language, signature
			FROM `tabCode Entity` WHERE `index` = %s""",
			(index,),
		)
		table = cls([_row(values) for values in result])
		for source, target in db.rows(
			"""SELECT source_entity, target_entity FROM `tabCode Edge`
			WHERE `index` = %s AND kind = 'inherits' AND target_entity IS NOT NULL AND target_entity != ''""",
			(index,),
		):
			table.bases[source].append(target)
		return table

	def add(self, row: EntityRow) -> None:
		self.rows[row.name] = row
		self.by_path[row.path].append(row)
		if row.kind == "file":
			self.files[row.path] = row
			self.modules[row.qualified_name] = row
			self.stems[_stem(row.path)][row.path] = row
			return
		self.by_qualified[row.qualified_name].append(row)
		self.by_name[row.entity_name].append(row)
		if row.parent_entity:
			self.children[row.parent_entity].setdefault(row.entity_name, row)

	def remove_paths(self, paths: set[str]) -> set[str]:
		"""Drop the entities of these paths. Return their names."""
		removed = {row.name for path in paths for row in self.by_path.get(path, [])}
		if removed:
			self.__init__([row for row in self.rows.values() if row.name not in removed])
		return removed

	# ------------------------------------------------------------- lookups

	def unique(
		self, name: str, kinds: tuple[str, ...] = CALLABLE, language: str | None = None
	) -> EntityRow | None:
		"""The only definition with this name, in the same language family when one is given."""
		matches = [row for row in self.by_name.get(name, []) if row.kind in kinds]
		if language:
			matches = [row for row in matches if family(row.language) == family(language)]
		return matches[0] if len(matches) == 1 else None

	def in_file(self, path: str, name: str, kinds: tuple[str, ...] = CALLABLE) -> EntityRow | None:
		file = self.files.get(path)
		if file and (row := self.children[file.name].get(name)) and row.kind in kinds:
			return row
		matches = [row for row in self.by_path.get(path, []) if row.entity_name == name and row.kind in kinds]
		return matches[0] if matches else None

	def member(self, class_name: str, member: str, depth: int = 3) -> EntityRow | None:
		"""A member of a class or of its bases."""
		row = self.children.get(class_name, {}).get(member)
		if row or depth == 0:
			return row
		for base in self.bases.get(class_name, []):
			if found := self.member(base, member, depth - 1):
				return found
		return None

	def enclosing_class(self, entity: str) -> EntityRow | None:
		row = self.rows.get(entity)
		while row and row.kind not in ("class", "interface"):
			row = self.rows.get(row.parent_entity or "")
		return row

	def module_file(
		self, module: str, from_path: str, language: str | None, relative: bool
	) -> EntityRow | None:
		"""The file an import points to."""
		for candidate in _module_candidates(module, from_path, language, relative):
			if candidate in self.files:
				return self.files[candidate]
			if candidate in self.modules:
				return self.modules[candidate]
		tail = module.replace("::", "/").replace("\\", "/").replace(".", "/").rsplit("/", 1)[-1]
		matches = [
			row for row in self.stems.get(tail, {}).values() if family(row.language) == family(language)
		]
		return matches[0] if len(matches) == 1 else None


def _stem(path: str) -> str:
	return posixpath.splitext(posixpath.basename(path))[0]


def _row(values: dict) -> EntityRow:
	return EntityRow(
		name=values["name"],
		kind=values["kind"] or "",
		entity_name=values["entity_name"] or "",
		qualified_name=values["qualified_name"] or "",
		path=values["path"] or "",
		parent_entity=values["parent_entity"],
		start_line=values["start_line"] or 0,
		end_line=values["end_line"] or 0,
		language=values["language"],
		signature=values["signature"] or "",
	)


JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".vue", "/index.ts", "/index.tsx", "/index.js")


def _module_candidates(module: str, from_path: str, language: str | None, relative: bool) -> list[str]:
	directory = posixpath.dirname(from_path)
	if language == "python":
		if relative:
			dots = len(module) - len(module.lstrip("."))
			base = from_path.rsplit("/", dots)[0] if dots else directory
			rest = module.lstrip(".").replace(".", "/")
			stem = posixpath.join(base, rest) if rest else base
		else:
			stem = module.replace(".", "/")
		return [f"{stem}.py", f"{stem}/__init__.py", module.lstrip(".")]
	if language in ("javascript", "typescript", "tsx", "vue"):
		if module.startswith("."):
			stem = posixpath.normpath(posixpath.join(directory, module))
		elif module.startswith("@/"):
			stem = "src/" + module[2:]
		else:
			return []
		return [stem, *[stem + ext for ext in JS_EXTENSIONS]]
	if language in ("java", "kotlin", "scala"):
		stem = module.replace(".", "/")
		return [
			f"{stem}.java",
			f"{stem}.kt",
			f"{stem}.scala",
			f"src/main/java/{stem}.java",
			f"src/main/kotlin/{stem}.kt",
		]
	if language in ("c", "cpp"):
		return [posixpath.normpath(posixpath.join(directory, module)), module]
	if language == "ruby":
		stem = posixpath.normpath(posixpath.join(directory, module)) if relative else module
		return [f"{stem}.rb", f"lib/{stem}.rb"]
	if language == "php":
		stem = module.replace("\\", "/")
		return [f"{stem}.php", f"src/{stem}.php", posixpath.normpath(posixpath.join(directory, module))]
	if language == "rust":
		parts = module.replace("crate::", "").replace("self::", "").split("::")
		stems = ["src/" + "/".join(parts), "src/" + "/".join(parts[:-1])]
		return [c for stem in stems if stem != "src/" for c in (f"{stem}.rs", f"{stem}/mod.rs")]
	return [module]
