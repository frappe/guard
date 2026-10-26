"""Validate a change against the other repositories of the team. See docs/spec/10-integrations.md.

Repositories are linked by code, not by setup: every indexed repository of the same team
counts, plus `context.repos` and Repository Clusters.

- Provider side: when a pull request deletes or changes a function, method or route, find
  where the other repositories use it: imports, Frappe method paths in strings and URLs,
  and HTTP calls that match the route.
- Consumer side: when a pull request adds a call to a method path or a route of another
  repository, check that the target exists there.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import cached_property

import frappe

from guard.git.diff import FileDiff
from guard.graph.overlay import TEST_PATH, ChangedEntity, ExternalCaller
from guard.indexing import languages, routes
from guard.indexing.parser import DOTTED_STRING
from guard.review.findings import Finding
from guard.utils import db

HeadReader = Callable[[str], str | None]
MAX_CALLERS = 20
FUNCTION_KINDS = ("function", "method", "class")
# A route family segment must be specific enough to say "this call is meant for that repository".
GENERIC_SEGMENTS = {"api", "v1", "v2", "v3", "method", "resource", "*"}
MIN_FAMILY_LENGTH = 4
# Files whose strings can call a method of another repository. A JSON contract or a doc can hold
# names like `admin.invites.get` that only look like Python paths.
CALLER_LANGUAGES = {"python", "javascript", "typescript", "tsx", "vue"}
# Tests call their own app, and some call a wrong path on purpose to get a 404.
NO_ROUTE_CHECK = re.compile(rf"{TEST_PATH.pattern}|(^|/)e2e/")
# `/api/method/<name>` is a Frappe method URL, which the method check covers, not a route.
METHOD_SEGMENTS = ("method",)


@dataclass
class OtherRepository:
	full_name: str
	repository: str
	index: str

	@cached_property
	def packages(self) -> set[str]:
		"""Top-level Python packages, for example `central` for frappe/central."""
		return {
			str(row[0])
			for row in db.rows(
				"""SELECT DISTINCT SUBSTRING_INDEX(qualified_name, '.', 1) FROM `tabCode Entity`
				WHERE `index` = %s AND kind = 'file' AND language = 'python'""",
				(self.index,),
			)
			if row[0]
		}

	@cached_property
	def routes(self) -> list[tuple[str, str, str, int]]:
		"""(verb, full normalized path, file path, line) of every route."""
		return routes_of(self.index)

	def defines(self, qualified_name: str) -> bool:
		return bool(
			db.scalar(
				"SELECT COUNT(*) FROM `tabCode Entity` WHERE `index` = %s AND qualified_name = %s",
				(self.index, qualified_name),
			)
		)

	def has_module(self, prefix: str) -> bool:
		"""A module or package of the repository is `prefix`, or is inside it."""
		return bool(
			db.scalar(
				"""SELECT COUNT(*) FROM `tabCode Entity` WHERE `index` = %s AND kind = 'file'
				AND (qualified_name = %s OR qualified_name LIKE %s)""",
				(self.index, prefix, f"{prefix}.%"),
			)
		)

	@cached_property
	def newer_head(self) -> str | None:
		"""The head of the default branch when it moved past the index, else None.

		The other repository can merge the method a minute before this review: the index then
		says that it does not exist. Read once, and only when a finding depends on it.
		"""
		from guard.git.repository import store_for
		from guard.git.store import GitError

		branch = str(db.value("Code Repository", self.repository, "default_branch") or "")
		indexed = str(db.value("Repository Index", self.index, "sha") or "")
		if not branch:
			return None
		try:
			store = store_for(self.repository)
			store.fetch_branches(branch)
			head = store.rev_parse(f"refs/heads/{branch}")
		except GitError:
			return None
		if head == indexed:
			return None
		from guard.indexing.jobs import enqueue_index

		enqueue_index(self.repository)
		return head

	def defines_at_head(self, qualified_name: str) -> bool:
		"""The newer head defines the Python path: its module exists and has the name."""
		from guard.git.repository import store_for

		head = self.newer_head
		if not head:
			return False
		store = store_for(self.repository)
		parts = qualified_name.split(".")
		for cut in range(len(parts), 0, -1):
			module, rest = "/".join(parts[:cut]), parts[cut:]
			for path in (f"{module}.py", f"{module}/__init__.py"):
				text = store.show(head, path)
				if text is None:
					continue
				return not rest or bool(_definition(rest[0]).search(text))
		return False


def linked(repository: str | None, provider: str | None, configured: list[str]) -> list[OtherRepository]:
	"""Indexed repositories to check: `context.repos`, cluster members, then the rest of the team."""
	found = {o.repository: o for o in related(repository, provider, configured)}
	team = db.value("Code Repository", repository, "team") if repository else None
	if team:
		for row in frappe.get_all(
			"Code Repository",
			filters={"team": team, "name": ["!=", repository], "default_index": ["is", "set"]},
			fields=["name", "full_name", "default_index"],
		):
			found.setdefault(row.name, OtherRepository(row.full_name, row.name, row.default_index))
	return [o for o in found.values() if db.value("Repository Index", o.index, "status") == "completed"]


def related(repository: str | None, provider: str | None, configured: list[str]) -> list[OtherRepository]:
	"""Repositories to read as context. `context.repos` come first, then cluster members."""
	names: list[str] = []
	for full_name in configured:
		name = (
			db.value("Code Repository", {"full_name": full_name, "code_provider": provider}, "name")
			if provider
			else None
		)
		if name:
			names.append(str(name))
	if repository:
		clusters = frappe.get_all(
			"Repository Cluster Member",
			filters={"repository": repository, "parenttype": "Repository Cluster"},
			pluck="parent",
		)
		for cluster in clusters:
			if db.value("Repository Cluster", cluster, "status") != "Active":
				continue
			names += frappe.get_all(
				"Repository Cluster Member", filters={"parent": cluster}, pluck="repository"
			)
	result = []
	for name in dict.fromkeys(n for n in names if n != repository):
		index = db.value("Code Repository", name, "default_index")
		if index:
			result.append(
				OtherRepository(str(db.value("Code Repository", name, "full_name")), name, str(index))
			)
	return result


def ensure_indexed(others: list[OtherRepository]) -> None:
	from guard.indexing.jobs import enqueue_index

	for other in others:
		if db.value("Repository Index", other.index, "status") != "completed":
			enqueue_index(other.repository)


# ------------------------------------------------------------------ provider side


def attach(changed: list[ChangedEntity], others: list[OtherRepository], own_index: str | None) -> None:
	"""Set `external` on each deleted or changed entity that other repositories use."""
	if not others:
		return
	own_routers = routers_of(own_index) if own_index else {}
	for entity in changed:
		if entity.status == "added" or not (entity.status == "deleted" or entity.contract_changed):
			continue
		if entity.kind in FUNCTION_KINDS:
			entity.external = _symbol_users(entity, others)
		elif entity.kind == "route":
			entity.external = _route_users(entity, others, own_routers)


def break_findings(changed: list[ChangedEntity]) -> list[Finding]:
	"""A P0 for each deleted entity that another repository surely still uses."""
	findings = []
	for entity in changed:
		broken = [caller for caller in entity.external if caller.breaks]
		if not broken or entity.status != "deleted" or not entity.base:
			continue
		uses = "\n".join(f"- `{c.label}`: `{c.text.strip()[:120]}`" for c in broken[:8])
		findings.append(
			Finding(
				entity.path,
				entity.base.start_line,
				entity.base.start_line,
				"P0",
				"logic",
				f"Removing `{entity.name}` breaks {len(broken)} use{'s' if len(broken) != 1 else ''} in other repositories",
				f"`{entity.qualified_name}` is removed, but other repositories of the team still use it:\n\n{uses}\n\n"
				"Update them first, or keep a compatible version until they move.",
				category="correctness",
				confidence=0.9,
				rule_id="cross-repo",
				side="LEFT",
			)
		)
	return findings


def _symbol_users(entity: ChangedEntity, others: list[OtherRepository]) -> list[ExternalCaller]:
	"""Imports and method paths that name the entity. A deleted entity surely breaks them."""
	by_index = {o.index: o for o in others}
	rows = db.dict_rows(
		"""SELECT `index`, path, line, kind, target_text FROM `tabCode Edge`
		WHERE `index` IN %(indexes)s AND target_name = %(name)s AND kind IN ('imports', 'references', 'calls')
		ORDER BY path, line LIMIT %(limit)s""",
		{"indexes": tuple(by_index), "name": entity.qualified_name, "limit": MAX_CALLERS},
	)
	breaks = True if entity.status == "deleted" else None
	return [
		ExternalCaller(
			by_index[row["index"]].full_name,
			by_index[row["index"]].repository,
			row["path"],
			int(row["line"] or 0),
			str(row["target_text"] or ""),
			"import" if row["kind"] == "imports" else "method",
			breaks,
		)
		for row in rows
	]


def _route_users(
	entity: ChangedEntity, others: list[OtherRepository], own_routers: dict[str, tuple[str, str]]
) -> list[ExternalCaller]:
	"""HTTP calls in other repositories that reach the route. Only strong matches surely break."""
	router, _, path = (entity.base.signature if entity.base else "").partition("|")
	verb = entity.name.split(" ", 1)[0]
	full = routes.full_path((router, path), own_routers)
	users = []
	for other in others:
		for row in db.dict_rows(
			"""SELECT path, line, target_name, target_text FROM `tabCode Edge`
			WHERE `index` = %s AND kind = 'references' AND target_name LIKE 'http:%%'""",
			(other.index,),
		):
			call_verb, _, call_path = str(row["target_name"])[5:].partition(" ")
			strength = routes.strength(full, call_path) if routes.verbs_match(verb, call_verb) else 0
			if strength:
				surely = entity.status == "deleted" and strength >= routes.STRONG
				users.append(
					ExternalCaller(
						other.full_name,
						other.repository,
						row["path"],
						int(row["line"] or 0),
						str(row["target_text"] or ""),
						"http",
						True if surely else None,
					)
				)
	return users[:MAX_CALLERS]


def routers_of(index: str) -> dict[str, tuple[str, str]]:
	"""Router name → (parent router, path segment), from `router` entities."""
	result = {}
	for name, signature in db.rows(
		"SELECT entity_name, signature FROM `tabCode Entity` WHERE `index` = %s AND kind = 'router'", (index,)
	):
		parent, _, segment = str(signature or "").partition("|")
		result[str(name)] = (parent, segment)
	return result


def routes_of(index: str) -> list[tuple[str, str, str, int]]:
	routers = routers_of(index)
	result = []
	for name, signature, path, line in db.rows(
		"SELECT entity_name, signature, path, start_line FROM `tabCode Entity` WHERE `index` = %s AND kind = 'route'",
		(index,),
	):
		router, _, raw = str(signature or "").partition("|")
		result.append(
			(str(name).split(" ", 1)[0], routes.full_path((router, raw), routers), str(path), int(line))
		)
	return result


def context_lines(changed: list[ChangedEntity], read: "LineReader") -> list[str]:
	"""Text for the reviewer: each external use with its line of code."""
	lines = []
	for entity in changed:
		for caller in entity.external[:8]:
			code = read(caller) or caller.text
			state = "surely breaks" if caller.breaks else "may break"
			lines.append(
				f"`{entity.qualified_name}` ({entity.status}) is used in {caller.label} ({caller.via}, {state}): {code.strip()[:200]}"
			)
	return lines


class LineReader:
	"""Read one line of another repository at its indexed commit."""

	def __init__(self):
		self.sources: dict[tuple[str, str], list[str]] = {}

	def __call__(self, caller: ExternalCaller) -> str:
		from guard.git.repository import store_for
		from guard.git.store import GitError

		key = (caller.repository, caller.path)
		if key not in self.sources:
			index = db.value("Code Repository", caller.repository, "default_index")
			sha = str(db.value("Repository Index", index, "sha") or "") if index else ""
			try:
				text = store_for(caller.repository).show(sha, caller.path) if sha else None
			except GitError:
				text = None
			self.sources[key] = (text or "").splitlines()
		lines = self.sources[key]
		return lines[caller.line - 1] if 0 < caller.line <= len(lines) else ""


# ------------------------------------------------------------------ consumer side


def missing_targets(
	files: list[FileDiff], head: "HeadReader", others: list[OtherRepository], own_index: str | None
) -> list[Finding]:
	"""New calls to method paths or routes of other repositories that do not exist there."""
	if not others:
		return []
	own_packages = OtherRepository("", "", own_index).packages if own_index else set()
	sources = {
		diff.path: str(head(diff.path) or "").splitlines()
		for diff in files
		if diff.status != "deleted" and not diff.is_binary
	}
	own_routes = _own_routes(own_index, sources)
	findings = []
	for diff in files:
		added = diff.added_lines() if diff.path in sources else set()
		if not added:
			continue
		lines = sources[diff.path]
		if languages.detect(diff.path) in CALLER_LANGUAGES:
			for number in sorted(added):
				if 0 < number <= len(lines):
					findings += _missing_methods(diff.path, number, lines[number - 1], others, own_packages)
		if NO_ROUTE_CHECK.search(diff.path):
			continue
		# A line that defines a route is not a call to one.
		defined = routes.defined_lines(routes.definitions(lines))
		for call in routes.client_calls(lines, defined):
			if call.covers(added):
				findings += _missing_route(diff.path, call, others, own_routes)
	return findings


def _own_routes(own_index: str | None, sources: dict[str, list[str]]) -> list[tuple[str, str, str, int]]:
	"""The routes of the repository under review: those of its index, and those that the pull
	request adds. A call to a route that the pull request adds is no call to another repository."""
	indexed = routes_of(own_index) if own_index else []
	table = routers_of(own_index) if own_index else {}
	for lines in sources.values():
		table.update({r.name: (r.parent, r.segment) for r in routes.routers(lines)})
	added = [
		(route.verb, routes.full_path(route, table), path, route.line)
		for path, lines in sources.items()
		for route in routes.definitions(lines)
	]
	return indexed + added


def _dotted_names(path: str, line: str) -> list[str]:
	names = [m.group(1) for m in DOTTED_STRING.finditer(line)]
	names += [m.group(1) for m in routes.METHOD_URL.finditer(line)]
	if path.endswith(".py"):
		for ref in languages.imports("python", line):
			names += [f"{ref.module}.{original}" for original in ref.names.values()]
	return list(dict.fromkeys(names))


def _missing_methods(
	path: str, number: int, line: str, others: list[OtherRepository], own_packages: set[str]
) -> list[Finding]:
	findings = []
	for name in _dotted_names(path, line):
		package = name.split(".", 1)[0]
		if package in own_packages:
			continue
		owner = next((o for o in others if package in o.packages), None)
		if owner is None or owner.defines(name) or _is_module(owner, name):
			continue
		# `admin.invites.get` is no Python path of a repository that has an `admin` package but no
		# `admin.invites`: the string names something else.
		if not owner.has_module(".".join(name.split(".")[:2])):
			continue
		if owner.defines_at_head(name):
			continue
		findings.append(
			Finding(
				path,
				number,
				number,
				"P1",
				"logic",
				f"`{name}` does not exist in {owner.full_name}",
				f"`{name}` is in the `{package}` package of `{owner.full_name}`, but its default branch "
				f"has no function, class or module with this path. The call fails at runtime. If it "
				f"comes from an open pull request in `{owner.full_name}`, merge that one first.",
				category="correctness",
				confidence=0.8,
				rule_id="cross-repo",
			)
		)
	return findings


def _is_module(owner: OtherRepository, name: str) -> bool:
	return bool(
		db.scalar(
			"SELECT COUNT(*) FROM `tabCode Entity` WHERE `index` = %s AND kind = 'file' AND qualified_name = %s",
			(owner.index, name),
		)
	)


def _missing_route(
	path: str,
	call: routes.ClientCall,
	others: list[OtherRepository],
	own_routes: list[tuple[str, str, str, int]],
) -> list[Finding]:
	"""A call that belongs to a route family of another repository but matches none of its routes."""
	literals = [s for s in call.normalized.split("/") if s != "*"]
	if literals[:1] and literals[0] in METHOD_SEGMENTS:
		return []
	family = next(
		(s for s in literals if s not in GENERIC_SEGMENTS and len(s) >= MIN_FAMILY_LENGTH and "*" not in s),
		None,
	)
	if family is None or len(literals) < routes.STRONG:
		return []
	if _reaches(own_routes, call):
		return []
	for other in others:
		in_family = [r for r in other.routes if family in r[1].split("/")]
		if not in_family or _reaches(other.routes, call):
			continue
		if other.newer_head:
			# The index is behind the repository: the route may be new. Do not guess.
			continue
		close = ", ".join(f"`{v} {p}`" for v, p, _, _ in in_family[:4])
		body = (
			f"`{other.full_name}` serves `{family}` routes, but none of them matches "
			f"`{call.verb} {call.normalized}`. Close ones: {close}. The call will get a 404 or 405."
		)
		return [
			Finding(
				path,
				call.line,
				call.line,
				"P1",
				"logic",
				f"No route in {other.full_name} matches {call.verb} {call.normalized}",
				body,
				category="correctness",
				confidence=0.7,
				rule_id="cross-repo",
			)
		]
	return []


def _definition(name: str) -> re.Pattern[str]:
	return re.compile(rf"^\s*(?:async\s+def|def|class)\s+{re.escape(name)}\b|^{re.escape(name)}\s*=", re.M)


def _reaches(defined: list[tuple[str, str, str, int]], call: routes.ClientCall) -> bool:
	return any(
		routes.verbs_match(v, call.verb) and routes.strength(p, call.normalized) for v, p, _, _ in defined
	)
