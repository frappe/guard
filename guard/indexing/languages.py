"""Language facts the parser needs: detection, imports, bases, call nodes."""

import re
from dataclasses import dataclass, field

import tree_sitter_language_pack as pack

EXTENSIONS = {
	".py": "python",
	".pyi": "python",
	".js": "javascript",
	".mjs": "javascript",
	".cjs": "javascript",
	".jsx": "javascript",
	".ts": "typescript",
	".mts": "typescript",
	".cts": "typescript",
	".tsx": "tsx",
	".vue": "vue",
	".go": "go",
	".rs": "rust",
	".java": "java",
	".kt": "kotlin",
	".kts": "kotlin",
	".rb": "ruby",
	".php": "php",
	".c": "c",
	".h": "c",
	".cc": "cpp",
	".cpp": "cpp",
	".cxx": "cpp",
	".hpp": "cpp",
	".hh": "cpp",
	".cs": "csharp",
	".swift": "swift",
	".scala": "scala",
	".lua": "lua",
	".ex": "elixir",
	".exs": "elixir",
	".dart": "dart",
}

# Languages with a module path in dotted form.
DOTTED_MODULES = {"python"}

CALL_NODE_TYPES = {
	"call",
	"call_expression",
	"method_invocation",
	"function_call",
	"invocation_expression",
	"method_call",
	"method_call_expression",
	"function_call_expression",
	"member_call_expression",
	"scoped_call_expression",
	"macro_invocation",
	"new_expression",
	"object_creation_expression",
}


@dataclass
class ImportRef:
	module: str
	line: int
	names: dict[str, str] = field(default_factory=dict)  # local alias -> imported name
	alias: str | None = None  # alias for the whole module
	relative: bool = False


def detect(path: str) -> str | None:
	lower = path.lower()
	for extension, language in EXTENSIONS.items():
		if lower.endswith(extension):
			return language
	return None


def has_grammar(language: str) -> bool:
	try:
		return bool(pack.has_language(language))
	except Exception:
		return False


# ---------------------------------------------------------------- imports

PY_FROM = re.compile(r"^\s*from\s+([.\w]+)\s+import\s+(.+)$")
PY_IMPORT = re.compile(r"^\s*import\s+([\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*)")
JS_FROM = re.compile(r"""^\s*(?:import|export)\s+(.*?)\s+from\s+['"]([^'"]+)['"]""")
JS_SIDE = re.compile(r"""^\s*import\s+['"]([^'"]+)['"]""")
JS_REQUIRE = re.compile(r"""(?:const|let|var)\s+(\{[^}]*\}|\w+)\s*=\s*require\(\s*['"]([^'"]+)['"]\s*\)""")
GO_IMPORT = re.compile(r"""^\s*(?:import\s+)?(?:(\w+|\.)\s+)?"([^"]+)"\s*$""")
JVM_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)(?:\.\*)?\s*;?\s*$")
RUST_USE = re.compile(r"^\s*(?:pub\s+)?use\s+([\w:]+)")
RUBY_REQUIRE = re.compile(r"""^\s*require(_relative)?\s+['"]([^'"]+)['"]""")
PHP_USE = re.compile(r"^\s*use\s+([\w\\]+)(?:\s+as\s+(\w+))?\s*;")
PHP_REQUIRE = re.compile(r"""(?:require|include)(?:_once)?\s*\(?\s*['"]([^'"]+)['"]""")
C_INCLUDE = re.compile(r"""^\s*#\s*include\s+"([^"]+)\"""")
CS_USING = re.compile(r"^\s*using\s+(?:static\s+)?([\w.]+)\s*;")
SWIFT_IMPORT = re.compile(r"^\s*import\s+(\w+)")


def imports(language: str, source: str) -> list[ImportRef]:
	parser = IMPORT_PARSERS.get(language)
	return parser(source.splitlines()) if parser else []


def _python(lines: list[str]) -> list[ImportRef]:
	out = []
	for number, line in enumerate(lines, 1):
		if match := PY_FROM.match(line):
			module, names = match.group(1), match.group(2).strip().strip("()")
			ref = ImportRef(module=module, line=number, relative=module.startswith("."))
			for part in names.split(","):
				name, _, alias = part.strip().partition(" as ")
				if name and name != "*":
					ref.names[(alias or name).strip()] = name.strip()
			out.append(ref)
		elif match := PY_IMPORT.match(line):
			for part in match.group(1).split(","):
				module, _, alias = part.strip().partition(" as ")
				out.append(ImportRef(module=module.strip(), line=number, alias=(alias or module).strip()))
	return out


def _javascript(lines: list[str]) -> list[ImportRef]:
	out = []
	for number, line in enumerate(lines, 1):
		if match := JS_FROM.match(line):
			out.append(_js_ref(match.group(2), match.group(1), number))
		elif match := JS_SIDE.match(line):
			out.append(ImportRef(module=match.group(1), line=number, relative=match.group(1).startswith(".")))
		for match in JS_REQUIRE.finditer(line):
			out.append(_js_ref(match.group(2), match.group(1), number))
	return out


def _js_ref(module: str, clause: str, line: int) -> ImportRef:
	ref = ImportRef(module=module, line=line, relative=module.startswith("."))
	clause = clause.strip()
	named = re.search(r"\{([^}]*)\}", clause)
	if named:
		for part in named.group(1).split(","):
			name, _, alias = part.strip().replace(":", " as ").partition(" as ")
			if name.strip():
				ref.names[(alias or name).strip()] = name.strip()
	star = re.search(r"\*\s+as\s+(\w+)", clause)
	if star:
		ref.alias = star.group(1)
	default = re.match(r"^(\w+)", clause)
	if default and default.group(1) not in ("type", "typeof"):
		ref.names[default.group(1)] = "default"
	return ref


def _go(lines: list[str]) -> list[ImportRef]:
	out, in_block = [], False
	for number, line in enumerate(lines, 1):
		stripped = line.strip()
		if stripped.startswith("import ("):
			in_block = True
			continue
		if in_block and stripped == ")":
			in_block = False
			continue
		if in_block or stripped.startswith("import "):
			match = GO_IMPORT.match(stripped)
			if match:
				path = match.group(2)
				out.append(
					ImportRef(module=path, line=number, alias=match.group(1) or path.rsplit("/", 1)[-1])
				)
	return out


def _simple(pattern: re.Pattern, alias_group: int | None = None, relative_group: int | None = None):
	def parse(lines: list[str]) -> list[ImportRef]:
		out = []
		for number, line in enumerate(lines, 1):
			for match in pattern.finditer(line):
				module = match.group(match.lastindex or 1) if relative_group else match.group(1)
				alias = match.group(alias_group) if alias_group else None
				relative = bool(match.group(relative_group)) if relative_group else False
				out.append(ImportRef(module=module, line=number, alias=alias, relative=relative))
		return out

	return parse


def _jvm(lines: list[str]) -> list[ImportRef]:
	out = []
	for number, line in enumerate(lines, 1):
		if match := JVM_IMPORT.match(line):
			module = match.group(1)
			out.append(
				ImportRef(
					module=module, line=number, names={module.rsplit(".", 1)[-1]: module.rsplit(".", 1)[-1]}
				)
			)
	return out


IMPORT_PARSERS = {
	"python": _python,
	"javascript": _javascript,
	"typescript": _javascript,
	"tsx": _javascript,
	"vue": _javascript,
	"go": _go,
	"java": _jvm,
	"kotlin": _jvm,
	"scala": _jvm,
	"rust": _simple(RUST_USE),
	"ruby": _simple(RUBY_REQUIRE, relative_group=1),
	"php": lambda lines: _simple(PHP_USE, alias_group=2)(lines) + _simple(PHP_REQUIRE)(lines),
	"c": _simple(C_INCLUDE),
	"cpp": _simple(C_INCLUDE),
	"csharp": _simple(CS_USING),
	"swift": _simple(SWIFT_IMPORT),
}

# ------------------------------------------------------------------- bases

PY_BASES = re.compile(r"^\s*class\s+\w+\s*\(([^)]*)\)")
EXTENDS = re.compile(r"\bextends\s+([\w.$<>, ]+?)(?:\s+implements\b|\s*\{|$)")
IMPLEMENTS = re.compile(r"\bimplements\s+([\w.$<>, ]+?)(?:\s*\{|$)")
RUBY_BASE = re.compile(r"^\s*class\s+\w+\s*<\s*([\w:]+)")
COLON_BASES = re.compile(r"\b(?:class|struct|interface|object)\s+\w+[^:{(]*(?:\([^)]*\))?\s*:\s*([^{]+)")
RUST_IMPL = re.compile(r"\bimpl\b[^{]*?\s([\w:]+)\s+for\s+")
NOISE = {"public", "private", "protected", "virtual", "object", "metaclass", "abc.ABC", "final", "open"}
COMMENT_LINE = re.compile(r"^\s*(#|//|/\*|\*)")


def clean_signature(signature: str) -> str:
	"""Drop comment lines that some parsers include before the body."""
	return "\n".join(line for line in signature.splitlines() if not COMMENT_LINE.match(line)).strip()


def bases(signature: str | None) -> list[str]:
	"""Base class and interface names from a class signature. The first matching style wins."""
	text = " ".join(clean_signature(signature or "").split())
	if not text:
		return []
	groups: list[str] = []
	if match := PY_BASES.search(text):
		groups = [match.group(1)]
	else:
		groups = [m.group(1) for pattern in (EXTENDS, IMPLEMENTS) for m in pattern.finditer(text)]
		for pattern in (RUBY_BASE, COLON_BASES, RUST_IMPL):
			if groups:
				break
			groups = [m.group(1) for m in pattern.finditer(text)]
	names: list[str] = []
	for group in groups:
		for part in re.split(r",", group):
			name = re.sub(r"[<(\[].*$", "", part.strip()).strip()
			name = name.split()[-1] if name.split() else ""
			name = name.split(".")[-1].split("::")[-1]
			if (
				name
				and "=" not in name
				and name not in NOISE
				and re.match(r"^[A-Za-z_$][\w$]*$", name)
				and name not in names
			):
				names.append(name)
	return names
