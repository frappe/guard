"""Parse one source file into entities and raw edges."""

import re
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import tree_sitter as ts
import tree_sitter_language_pack as pack

from guard.indexing import languages, routes
from guard.indexing.languages import ImportRef

PARSER_VERSION = 2
KINDS = {
	"class": "class",
	"struct": "class",
	"enum": "type",
	"trait": "interface",
	"interface": "interface",
	"impl": "class",
	"module": "module",
	"namespace": "module",
	"function": "function",
	"method": "method",
	"constructor": "method",
	"type": "type",
	"typealias": "type",
	"variable": "variable",
	"constant": "variable",
}
DOTTED_STRING = re.compile(r"""['"]([a-z_][\w]*(?:\.[\w]+){2,})['"]""")
SCRIPT_BLOCK = re.compile(r"<script[^>]*>(.*?)</script>", re.DOTALL)
MEMBER_TYPES = {
	"attribute",
	"member_expression",
	"field_expression",
	"selector_expression",
	"scoped_identifier",
	"member_access_expression",
	"navigation_expression",
	"field_access",
	"call",
	"method_invocation",
	"member_call_expression",
	"scoped_call_expression",
	"method_call",
}


@dataclass
class ParsedEntity:
	key: str
	kind: str
	name: str
	qualified_name: str
	parent_key: str | None
	start_line: int
	end_line: int
	signature: str = ""
	docstring: str = ""
	decorators: list[str] = field(default_factory=list)
	is_exported: bool = False
	is_entry_point: bool = False


@dataclass
class RawEdge:
	source_key: str
	kind: str  # contains, imports, calls, inherits, references
	target_name: str
	target_text: str
	line: int
	target_key: str | None = None  # already known, for contains


@dataclass
class ParsedFile:
	path: str
	language: str | None
	module: str
	line_count: int
	entities: list[ParsedEntity] = field(default_factory=list)
	edges: list[RawEdge] = field(default_factory=list)
	imports: list[ImportRef] = field(default_factory=list)

	@property
	def file_entity(self) -> ParsedEntity:
		return self.entities[0]

	def innermost(self, line: int) -> ParsedEntity:
		"""The smallest entity that contains the line, else the file."""
		best = self.file_entity
		for entity in self.entities[1:]:
			if entity.start_line <= line <= entity.end_line and (
				entity.end_line - entity.start_line <= best.end_line - best.start_line
			):
				best = entity
		return best


def module_path(path: str, language: str | None) -> str:
	stem = re.sub(r"\.[^./]+$", "", path)
	if language in languages.DOTTED_MODULES:
		stem = re.sub(r"(^|/)__init__$", "", stem)
		return stem.replace("/", ".")
	return stem


def parse_file(path: str, source: str) -> ParsedFile:
	language = languages.detect(path)
	lines = source.count("\n") + 1
	parsed = ParsedFile(path=path, language=language, module=module_path(path, language), line_count=lines)
	parsed.entities.append(ParsedEntity(path, "file", path.rsplit("/", 1)[-1], parsed.module, None, 1, lines))
	if not language:
		return parsed
	code, offset, code_language = source, 0, language
	if language == "vue":
		code, offset, code_language = _vue_script(source)
	if code_language and languages.has_grammar(code_language):
		_Extractor(parsed, code, offset, code_language).run()
	parsed.imports = languages.imports(language, source)
	for ref in parsed.imports:
		parsed.edges.append(RawEdge(path, "imports", ref.module, ref.module, ref.line))
		# One edge per imported name, so `from a.b import c` is found as `a.b.c` by other repositories.
		for original in dict.fromkeys(ref.names.values()):
			target = f"{ref.module}.{original}"
			parsed.edges.append(RawEdge(path, "imports", target, target, ref.line))
	_string_references(parsed, source)
	_http(parsed, source)
	return parsed


class _Extractor:
	def __init__(self, parsed: ParsedFile, code: str, offset: int, language: str):
		self.parsed = parsed
		self.code = code
		self.code_bytes = code.encode()
		self.lines = code.splitlines()
		self.offset = offset
		self.language = language

	def run(self) -> None:
		structure = self._structure()
		for item in structure:
			self._add_item(item, self.parsed.file_entity)
		if len(self.parsed.entities) == 1:
			self._definitions_from_tags()
		self._calls()

	def _structure(self) -> list[Any]:
		try:
			config = pack.ProcessConfig(
				language=self.language, structure=True, imports=False, exports=True, docstrings=True
			)
			result = pack.process(self.code, config)
		except Exception:
			return []
		self.exports = {item.name for item in result.exports or [] if item.name}
		return list(result.structure or [])

	def _add_item(self, item: Any, parent: ParsedEntity) -> None:
		kind = KINDS.get(str(item.kind).lower().split(".")[-1])
		if not item.name or not kind or (kind == "module" and parent.kind != "file"):
			for child in item.children or []:
				self._add_item(child, parent)
			return
		if kind == "function" and parent.kind in ("class", "interface"):
			kind = "method"
		start = item.span.start_line + 1 + self.offset
		end = item.span.end_line + 1 + self.offset
		qualified = f"{parent.qualified_name}.{item.name}" if parent.qualified_name else item.name
		decorators = [str(d) for d in item.decorators or []] or self._decorators_above(item.span.start_line)
		entity = ParsedEntity(
			key=f"{qualified}@{start}",
			kind=kind,
			name=item.name,
			qualified_name=qualified,
			parent_key=parent.key,
			start_line=start,
			end_line=end,
			signature=languages.clean_signature(item.signature or "")[:500],
			docstring=(item.doc_comment or "")[:500],
			decorators=decorators,
			is_exported=item.name in getattr(self, "exports", set()) or not item.name.startswith("_"),
		)
		self.parsed.entities.append(entity)
		self.parsed.edges.append(RawEdge(parent.key, "contains", item.name, item.name, start, entity.key))
		if kind in ("class", "interface"):
			for base in languages.bases(entity.signature):
				self.parsed.edges.append(RawEdge(entity.key, "inherits", base, base, start))
		for child in item.children or []:
			self._add_item(child, entity)

	def _decorators_above(self, row: int) -> list[str]:
		"""Decorator lines right above a definition. The structure API leaves them out."""
		found: list[str] = []
		row -= 1
		while 0 <= row < len(self.lines) and self.lines[row].strip().startswith("@"):
			found.insert(0, self.lines[row].strip())
			row -= 1
		return found

	def _definitions_from_tags(self) -> None:
		query = _tags_query(self.language)
		if not query:
			return
		tree = _parser(self.language).parse(self.code.encode())
		for _, captures in ts.QueryCursor(query).matches(tree.root_node):
			definition = next((k for k in captures if k.startswith("definition.")), None)
			names = captures.get("name")
			if not definition or not names:
				continue
			node, name = captures[definition][0], _text(names[0])
			kind = KINDS.get(definition.split(".", 1)[1], "function")
			start = node.start_point[0] + 1 + self.offset
			qualified = f"{self.parsed.module}.{name}"
			entity = ParsedEntity(
				f"{qualified}@{start}",
				kind,
				name,
				qualified,
				self.parsed.path,
				start,
				node.end_point[0] + 1 + self.offset,
			)
			self.parsed.entities.append(entity)

	def _calls(self) -> None:
		tree = _parser(self.language).parse(self.code.encode())
		seen: set[tuple[int, str]] = set()
		query = _tags_query(self.language)
		if query:
			for _, captures in ts.QueryCursor(query).matches(tree.root_node):
				if "reference.call" not in captures or "name" not in captures:
					continue
				node, name_node = captures["reference.call"][0], captures["name"][0]
				self._add_call(_text(name_node), self._callee(name_node), node.start_point[0], seen)
		if not seen:
			for node in _walk(tree.root_node):
				if node.type in languages.CALL_NODE_TYPES:
					callee = (
						node.child_by_field_name("function")
						or node.child_by_field_name("name")
						or node.child(0)
					)
					if callee is not None:
						text = _text(callee)
						name = re.split(r"[.:>\->]+", text)[-1]
						self._add_call(name, text, node.start_point[0], seen)

	def _callee(self, name: ts.Node) -> str:
		"""Callee text with its receiver: `self.save`, `Foo().bar`, `this.repo.save`."""
		parent = name.parent
		start = parent.start_byte if parent is not None and parent.type in MEMBER_TYPES else name.start_byte
		return self.code_bytes[start : name.end_byte].decode("utf-8", "replace")

	def _add_call(self, name: str, text: str, row: int, seen: set[tuple[int, str]]) -> None:
		name = name.strip()
		if not name or not re.match(r"^[A-Za-z_$][\w$]*$", name) or (row, name) in seen:
			return
		seen.add((row, name))
		line = row + 1 + self.offset
		source = self.parsed.innermost(line)
		self.parsed.edges.append(RawEdge(source.key, "calls", name, text[:200], line))


def _string_references(parsed: ParsedFile, source: str) -> None:
	for number, line in enumerate(source.splitlines(), 1):
		for match in DOTTED_STRING.finditer(line):
			owner = parsed.innermost(number)
			parsed.edges.append(RawEdge(owner.key, "references", match.group(1), match.group(1), number))


def _http(parsed: ParsedFile, source: str) -> None:
	"""Routes, routers, HTTP client calls and Frappe method URLs. See guard/indexing/routes.py."""
	lines = source.splitlines()
	defined = routes.definitions(lines)
	for route in defined:
		qualified = f"route:{route.verb} {route.router}:{routes.normalize(route.path)}"
		parsed.entities.append(
			ParsedEntity(
				key=f"{qualified}@{route.line}",
				kind="route",
				name=f"{route.verb} {route.path or '/'}",
				qualified_name=qualified,
				parent_key=parsed.file_entity.key,
				start_line=route.line,
				end_line=route.line,
				# The router name and raw path. The full path is resolved when the graph is read.
				signature=f"{route.router}|{route.path}",
				is_entry_point=True,
			)
		)
	for router in routes.routers(lines):
		parsed.entities.append(
			ParsedEntity(
				key=f"router:{router.name}@{router.line}",
				kind="router",
				name=router.name,
				qualified_name=f"router:{parsed.module}.{router.name}",
				parent_key=parsed.file_entity.key,
				start_line=router.line,
				end_line=router.line,
				signature=f"{router.parent}|{router.segment}",
			)
		)
	for call in routes.client_calls(lines, {r.line for r in defined}):
		owner = parsed.innermost(call.line)
		target = f"http:{call.verb} {call.normalized}"
		parsed.edges.append(RawEdge(owner.key, "references", target, call.text, call.line))
	for dotted, number in routes.method_urls(lines):
		owner = parsed.innermost(number)
		parsed.edges.append(RawEdge(owner.key, "references", dotted, dotted, number))


def _vue_script(source: str) -> tuple[str, int, str]:
	match = SCRIPT_BLOCK.search(source)
	if not match:
		return "", 0, ""
	offset = source[: match.start(1)].count("\n")
	header = source[match.start() : match.start(1)]
	language = "typescript" if 'lang="ts"' in header or "lang='ts'" in header else "javascript"
	return match.group(1), offset, language


def _text(node: ts.Node) -> str:
	return (node.text or b"").decode("utf-8", "replace")


def _walk(node: ts.Node):
	stack = [node]
	while stack:
		current = stack.pop()
		yield current
		stack.extend(current.children)


@cache
def _parser(language: str) -> ts.Parser:
	return pack.get_parser(language)  # type: ignore[arg-type]


@cache
def _tags_query(language: str) -> ts.Query | None:
	try:
		text = pack.get_tags_query(language)  # type: ignore[arg-type]
		return ts.Query(pack.get_language(language), text) if text else None  # type: ignore[arg-type]
	except Exception:
		return None
