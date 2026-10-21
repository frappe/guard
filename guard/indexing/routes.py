"""HTTP coupling between repositories, read from source lines.

- Route definitions: `@router.post("<id>/actions/start")`, `@app.get("/users")`,
  `router.get("/users", ...)`, `mux.HandleFunc("/users", ...)`.
- Routers with a path prefix: `vms = api.subrouter("virtual-machines")`,
  `router = APIRouter(prefix="/users")`, `bp = Blueprint("x", __name__, url_prefix="/x")`.
  A route keeps the name of its router. The full path is resolved when the graph is read,
  because the router can be defined in another file.
- Client calls: the path argument that comes after an HTTP verb, for example
  `self._request("POST", f"virtual-machines/{name}/actions/stop")`, `requests.get("/users")`
  or `fetch("/api/users")`.
- Frappe method URLs: `/api/method/app.module.function` in any string.

Paths are normalized: parameters become `*`, so `<id>`, `{name}`, `:id` and `${id}` match.
"""

import re
from dataclasses import dataclass
from fnmatch import fnmatchcase

VERBS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD")
ANY = "ANY"
ROUTE_DEF = re.compile(
	r"""(?:^\s*@\s*([\w.]*?)\.|\b(app|router|api|mux|group)\.)"""
	r"""(get|post|put|patch|delete|head|route|api_route|handle|handlefunc)\(\s*f?(['"`])([^'"`\n]*)\4""",
	re.I,
)
ROUTER_CHILD = re.compile(r"""^\s*(\w+)\s*=\s*([\w.]+)\.(?:subrouter|child|group|router)\(""")
FIRST_STRING = re.compile(r"""^\s*f?(['"])([^'"\n]*)\1""")
ROUTER_NEW = re.compile(r"""^\s*(\w+)\s*=\s*(?:[\w.]+\.)?(?:Router|APIRouter|Blueprint|Namespace)\(""")
PREFIX_ARG = re.compile(r"""\b(?:prefix|url_prefix|path)\s*=\s*(?:f?(['"])([^'"\n]*)\1|([A-Z_][A-Z0-9_]*))""")
CONSTANT = re.compile(r"""^\s*([A-Z_][A-Z0-9_]*)\s*=\s*(['"])([^'"\n]*)\2\s*$""")
VERB_LITERAL = re.compile(r"""(['"])(GET|POST|PUT|PATCH|DELETE|HEAD)\1\s*,\s*""")
VERB_CALL = re.compile(
	r"""(\w*)\.(get|post|put|patch|delete|head)\(\s*|\b(?:fetch|axios|request)\(\s*""", re.I
)
HTTP_CLIENT = re.compile(r"(?:requests|httpx|session|client|http|api|axios|\$http)$", re.I)
# A string literal. Other quote characters may appear inside, as in f"a/{quote(x, safe='')}".
STRING_AT = re.compile(r"""f?(['"`])((?:(?!\1)[^\n])+)\1""")
NAME_AT = re.compile(r"""([A-Za-z_]\w*)\s*[,)]""")
LOOKBACK = 20
CONTINUED = 3
# An interpolated expression, one level of nested braces deep: {quote(x, safe='')}, ${a.b}.
EXPRESSION = re.compile(r"\$?\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}")
METHOD_URL = re.compile(r"/api/(?:v\d/)?method/([a-z_]\w*(?:\.\w+){2,})")
PARAM = re.compile(r"^(?:<[^>]*>|\{[^}]*\}|\$\{[^}]*\}|:\w+|\*)$")
NOT_A_PATH = re.compile(
	r"\s|^\w+://|^[A-Z]+$|\.(?:py|js|ts|json|md|html|css|vue|go|txt|svg|png|sh|yml|yaml|toml)$", re.I
)
# Prefixes that clients add and routers may not repeat.
PREFIXES = ("api", "v1", "v2", "v3")
LOOKAHEAD = 8
STRONG = 2


@dataclass
class RouteDef:
	verb: str
	router: str
	path: str
	line: int


@dataclass
class RouterDef:
	name: str
	parent: str
	segment: str
	line: int


@dataclass
class ClientCall:
	verb: str
	normalized: str
	text: str
	line: int
	end_line: int = 0

	def covers(self, lines: set[int]) -> bool:
		return any(n in lines for n in range(self.line, max(self.end_line, self.line) + 1))


def segments(path: str) -> list[str]:
	"""`/api/vms/{id}/stop?x=1` → ["vms", "*", "stop"]. Leading api and version parts go."""
	path = re.sub(r"[?#].*$", "", EXPRESSION.sub("{}", path.strip()))
	parts = [p for p in re.split(r"/+", path) if p]
	out = [_segment(p) for p in parts]
	while out and out[0] in PREFIXES:
		out.pop(0)
	return out


def _segment(part: str) -> str:
	"""`{id}` → `*`, `public-ipv{v}` → `public-ipv*`, `Users` → `users`."""
	if PARAM.match(part):
		return "*"
	value = re.sub(r"\{\}|<[^>]*>|:\w+$", "*", part).lower()
	return "*" if set(value) <= {"*"} else value


def normalize(path: str) -> str:
	return "/".join(segments(path))


def strength(route: str, call: str) -> int:
	"""How many literal segments a client path shares with a route, compared from the end.

	0 means no match. Routers and clients add different prefixes, so only the overlap counts.
	A match must cover every literal of the call in the overlap, up to STRONG of them.
	"""
	r, c = route.split("/") if route else [], call.split("/") if call else []
	if not r or not c:
		return 0
	equal = needed = 0
	for a, b in zip(reversed(r), reversed(c), strict=False):
		if b != "*":
			needed += 1
		if a == "*" or b == "*":
			continue
		if a != b and not fnmatchcase(a, b) and not fnmatchcase(b, a):
			return 0
		equal += 1
	return equal if equal and equal >= min(STRONG, needed) else 0


def verbs_match(route_verb: str, call_verb: str) -> bool:
	return ANY in (route_verb, call_verb) or route_verb == call_verb


def full_path(route: RouteDef | tuple[str, str], routers: dict[str, tuple[str, str]]) -> str:
	"""Prefix a route with the segments of its router chain. `routers`: name → (parent, segment)."""
	router, path = (route.router, route.path) if isinstance(route, RouteDef) else route
	parts, seen = [path], set()
	while router in routers and router not in seen:
		seen.add(router)
		parent, segment = routers[router]
		parts.insert(0, segment)
		router = parent
	return normalize("/".join(p.strip("/") for p in parts if p.strip("/")))


def definitions(lines: list[str]) -> list[RouteDef]:
	found = []
	for number, line in enumerate(lines, 1):
		for match in ROUTE_DEF.finditer(line):
			router = (match.group(1) or match.group(2) or "").rsplit(".", 1)[-1]
			word, path = match.group(3).upper(), match.group(5)
			if NOT_A_PATH.search(path.strip()) and path.strip():
				continue
			found.append(RouteDef(word if word in VERBS else ANY, router, path, number))
	return found


def routers(lines: list[str]) -> list[RouterDef]:
	constants = {m.group(1): m.group(3) for line in lines if (m := CONSTANT.match(line))}
	found = []
	for number, line in enumerate(lines, 1):
		if match := ROUTER_CHILD.match(line):
			rest = " ".join([line[match.end() :], *lines[number : number + LOOKAHEAD]])
			first = FIRST_STRING.match(rest)
			parent = match.group(2).rsplit(".", 1)[-1]
			found.append(RouterDef(match.group(1), parent, first.group(2) if first else "", number))
		elif match := ROUTER_NEW.match(line):
			window = " ".join(lines[number - 1 : number - 1 + LOOKAHEAD])
			prefix = PREFIX_ARG.search(window)
			segment = (prefix.group(2) or constants.get(prefix.group(3) or "", "")) if prefix else ""
			found.append(RouterDef(match.group(1), "", segment or "", number))
	return found


def client_calls(lines: list[str], defined: set[int]) -> list[ClientCall]:
	"""The path argument after an HTTP verb. Lines that define routes are skipped."""
	found = []
	consumed = 0
	for number, line in enumerate(lines, 1):
		if number in defined or number <= consumed:
			continue
		text, consumed = _logical_line(lines, number)
		for verb, start, strict in _verb_positions(text):
			path = _argument(lines, number, text, start)
			if path is None:
				continue
			bare = EXPRESSION.sub("{}", path).strip()
			if NOT_A_PATH.search(bare) or (strict and "/" not in bare):
				continue
			normalized = normalize(path)
			if any(s != "*" for s in normalized.split("/")):
				found.append(ClientCall(verb, normalized, line.strip()[:200], number, consumed))
	return found


def method_urls(lines: list[str]) -> list[tuple[str, int]]:
	return [(m.group(1), n) for n, line in enumerate(lines, 1) for m in METHOD_URL.finditer(line)]


def _logical_line(lines: list[str], number: int) -> tuple[str, int]:
	"""The line, joined with the next ones while it ends inside an open call, and the last line used."""
	text, last = lines[number - 1].rstrip(), number
	for extra in lines[number : number + CONTINUED]:
		if not text.endswith(("(", ",")):
			break
		text += " " + extra.strip()
		last += 1
	return text, last


def _argument(lines: list[str], number: int, line: str, start: int) -> str | None:
	"""The path at `start`: a string literal, or a variable set to one in the lines before."""
	if literal := STRING_AT.match(line, start):
		return literal.group(2)
	name = NAME_AT.match(line, start)
	if not name or name.group(1) in ("self", "None", "url"):
		return None
	assign = re.compile(rf"""^\s*{re.escape(name.group(1))}\s*=\s*""")
	for previous in reversed(lines[max(number - 1 - LOOKBACK, 0) : number - 1]):
		if found := assign.match(previous):
			literal = STRING_AT.match(previous, found.end())
			return literal.group(2) if literal else None
	return None


def _verb_positions(line: str) -> list[tuple[str, int, bool]]:
	"""(verb, offset of the path argument, strict) for each HTTP call on a line.

	`x.get("...")` is also a dict lookup, so unless `x` looks like an HTTP client the path
	must contain a slash (strict).
	"""
	positions = [(m.group(2), m.end(), False) for m in VERB_LITERAL.finditer(line)]
	for match in VERB_CALL.finditer(line):
		word = (match.group(2) or "").upper()
		strict = bool(word) and not HTTP_CLIENT.search(match.group(1) or "")
		positions.append((word if word in VERBS else ANY, match.end(), strict))
	return positions
