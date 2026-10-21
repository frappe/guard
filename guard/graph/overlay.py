"""The graph of a pull request: head entities laid over the base index.

The base index describes the base branch. The overlay parses changed files at the
head in memory, finds the changed entities, and reads their callers from the base.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from guard.git.diff import FileDiff
from guard.graph import calls
from guard.graph.queries import CodeGraph, Entity, Link
from guard.indexing import frappe_plugin
from guard.indexing.parser import ParsedEntity, ParsedFile, parse_file

CHANGE_KINDS = ("function", "method", "class", "interface", "type", "doctype")
TEST_PATH = re.compile(r"(^|/)(tests?|__tests__|spec)/|(^|/)test_[^/]+$|_test\.\w+$|\.(test|spec)\.\w+$")
SNIPPET_RADIUS = 3


@dataclass
class CallSite:
	link: Link
	snippet: str


@dataclass
class ChangedEntity:
	path: str
	kind: str
	name: str
	qualified_name: str
	status: str  # added, modified, deleted
	head_start: int = 0
	head_end: int = 0
	head_signature: str = ""
	base: Entity | None = None
	head_body: str = ""
	base_body: str = ""
	callers: list[Link] = field(default_factory=list)
	not_updated: list[Link] = field(default_factory=list)
	at_risk: list[CallSite] = field(default_factory=list)
	callees: list[str] = field(default_factory=list)
	siblings: list[Entity] = field(default_factory=list)
	tests: list[str] = field(default_factory=list)

	@property
	def contract_changed(self) -> bool:
		if self.status == "deleted":
			return True
		if not self.base or self.status == "added":
			return False
		return _normalize(self.base.signature) != _normalize(self.head_signature)

	@property
	def label(self) -> str:
		return f"{self.qualified_name} [{self.status}{', contract changed' if self.contract_changed else ''}]"


@dataclass
class BlastRadius:
	callers: int
	files: int
	not_updated: int
	tests: list[str]
	untested: list[str]

	def sentence(self) -> str:
		if not self.callers:
			return ""
		text = f"Affects {self.callers} caller{'s' if self.callers != 1 else ''} in {self.files} file{'s' if self.files != 1 else ''}."
		if self.not_updated:
			text += f" {self.not_updated} caller{'s' if self.not_updated != 1 else ''} may break."
		return text


Reader = Callable[[str], str | None]


class PullRequestGraph:
	"""`head` and `base` read a file at the head and at the merge base."""

	def __init__(
		self,
		graph: CodeGraph | None,
		head: Reader,
		base: Reader,
		files: list[FileDiff],
		frappe_app: bool = False,
	):
		self.graph = graph
		self.head = head
		self.base = base
		self.files = [f for f in files if not f.is_binary]
		self.by_path = {f.path: f for f in self.files}
		self.by_old_path = {f.old_path: f for f in self.files}
		self.frappe_app = frappe_app
		self._base_sources: dict[str, list[str]] = {}

	def build(self) -> list[ChangedEntity]:
		changed: list[ChangedEntity] = []
		for diff in self.files:
			if diff.status == "deleted":
				changed += self._deleted_file(diff)
				continue
			source = self.head(diff.path)
			if source is None:
				continue
			parsed = parse_file(diff.path, source)
			if self.frappe_app:
				frappe_plugin.augment(parsed, source)
			changed += self._changed_in_file(diff, parsed, source.splitlines())
		for entity in changed:
			self._enrich(entity)
		return changed

	def blast_radius(self, changed: list[ChangedEntity]) -> BlastRadius:
		callers = [link for entity in changed for link in entity.callers]
		tests = sorted({path for entity in changed for path in entity.tests})
		untested = [
			e.qualified_name
			for e in changed
			if e.kind in ("function", "method")
			and e.status != "deleted"
			and not e.tests
			and not e.name.startswith("_")
			and not TEST_PATH.search(e.path)
		]
		return BlastRadius(
			callers=len(callers),
			files=len({link.path for link in callers}),
			not_updated=sum(len(e.not_updated) for e in changed),
			tests=tests,
			untested=untested,
		)

	# ------------------------------------------------------------ changes

	def _changed_in_file(self, diff: FileDiff, parsed: ParsedFile, lines: list[str]) -> list[ChangedEntity]:
		added = diff.added_lines()
		results: dict[str, ChangedEntity] = {}
		for line in sorted(added):
			entity = _innermost(parsed, line)
			if not entity or entity.key in results:
				continue
			results[entity.key] = ChangedEntity(
				path=diff.path,
				kind=entity.kind,
				name=entity.name,
				qualified_name=entity.qualified_name,
				status="modified",
				head_start=entity.start_line,
				head_end=entity.end_line,
				head_signature=entity.signature,
				head_body="\n".join(lines[entity.start_line - 1 : entity.end_line]),
			)
		results.update({f"removed:{e.qualified_name}": e for e in self._removed(diff, parsed)})
		return list(results.values())

	def _removed(self, diff: FileDiff, parsed: ParsedFile) -> list[ChangedEntity]:
		"""Base entities of a modified file that the head no longer has."""
		if not self.graph:
			return []
		head_names = {e.qualified_name for e in parsed.entities}
		base_path = diff.old_path or diff.path
		removed = []
		for entity in self.graph.outline(base_path):
			if (
				entity.kind in CHANGE_KINDS
				and entity.qualified_name not in head_names
				and _renamed(entity.qualified_name, diff, parsed) not in head_names
			):
				removed.append(
					ChangedEntity(
						base_path,
						entity.kind,
						entity.entity_name,
						entity.qualified_name,
						"deleted",
						base=entity,
					)
				)
		return removed

	def _deleted_file(self, diff: FileDiff) -> list[ChangedEntity]:
		if not self.graph:
			return []
		return [
			ChangedEntity(
				diff.old_path or diff.path, e.kind, e.entity_name, e.qualified_name, "deleted", base=e
			)
			for e in self.graph.outline(diff.old_path or diff.path)
			if e.kind in CHANGE_KINDS
		]

	# ------------------------------------------------------------- enrich

	def _enrich(self, entity: ChangedEntity) -> None:
		if not self.graph:
			return
		if entity.base is None:
			matches = [
				d
				for d in self.graph.definitions(entity.qualified_name)
				if d.qualified_name == entity.qualified_name
			]
			entity.base = matches[0] if matches else None
			if entity.base is None:
				entity.status = "added"
		if entity.base:
			entity.base_body = self._base_lines(
				entity.base.path, entity.base.start_line, entity.base.end_line
			)
			entity.callers = self.graph.callers(entity.base)
			entity.callees = [link.name for link in self.graph.callees(entity.base)][:40]
			entity.siblings = self.graph.siblings(entity.base, limit=4)
			entity.tests = sorted({link.path for link in _test_links(self.graph, entity.base)})
		entity.not_updated = [
			link for link in entity.callers if not self._updated(link) and self._breaks(entity, link)
		]
		if entity.contract_changed:
			entity.at_risk = [
				CallSite(
					link,
					self._base_lines(
						link.path, link.line - SNIPPET_RADIUS, link.line + SNIPPET_RADIUS, number=True
					),
				)
				for link in entity.not_updated[:12]
			]

	def _breaks(self, entity: ChangedEntity, link: Link) -> bool:
		"""True when the call may not fit the head signature. Unknown counts as a break."""
		if entity.status == "deleted":
			return True
		if not entity.contract_changed:
			return False
		params = calls.parse_params(entity.head_signature)
		call = calls.parse_call(self._base_source(link.path), link.line, entity.name) if params else None
		return not (params and call and calls.fits(params, call))

	def _updated(self, link: Link) -> bool:
		"""True when the pull request changes the line of the call."""
		diff = self.by_path.get(link.path) or self.by_old_path.get(link.path)
		if not diff:
			return False
		if diff.status == "deleted":
			return True
		return any(
			line.kind == "-" and line.old_no == link.line for hunk in diff.hunks for line in hunk.lines
		)

	def _base_source(self, path: str) -> list[str]:
		if path not in self._base_sources:
			self._base_sources[path] = (self.base(path) or "").splitlines()
		return self._base_sources[path]

	def _base_lines(self, path: str, start: int, end: int, number: bool = False) -> str:
		lines = self._base_source(path)
		start = max(start, 1)
		chosen = lines[start - 1 : max(end, start)]
		if number:
			return "\n".join(f"{start + i:>5}  {text}" for i, text in enumerate(chosen))
		return "\n".join(chosen)


def _test_links(graph: CodeGraph, entity: Entity) -> list[Link]:
	impact = graph.impact([entity], depth=2)
	return [link for links in impact.files.values() for link in links if TEST_PATH.search(link.path)]


def _innermost(parsed: ParsedFile, line: int) -> ParsedEntity | None:
	best = None
	for entity in parsed.entities[1:]:
		if entity.kind in CHANGE_KINDS and entity.start_line <= line <= entity.end_line:
			if best is None or entity.end_line - entity.start_line < best.end_line - best.start_line:
				best = entity
	return best


def _renamed(qualified: str, diff: FileDiff, parsed: ParsedFile) -> str:
	"""Qualified name after a file rename, so moved entities are not reported as removed."""
	if diff.status != "renamed" or not diff.old_path:
		return qualified
	old_module = parse_file(diff.old_path, "").module
	return qualified.replace(old_module, parsed.module, 1) if qualified.startswith(old_module) else qualified


def _normalize(signature: str) -> str:
	return re.sub(r"\s+", " ", signature or "").strip()


def impact_diagram(changed: list[ChangedEntity], limit: int = 12) -> str:
	"""A Mermaid graph of changed entities and their callers, from the graph alone."""
	lines = ["flowchart LR"]
	ids: dict[str, str] = {}

	def node(label: str, style: str = "") -> str:
		if label not in ids:
			ids[label] = f"n{len(ids)}"
			text = label.replace('"', "'")[:60]
			lines.append(f'  {ids[label]}["{text}"]{style}')
		return ids[label]

	for entity in changed[:limit]:
		if entity.kind not in ("function", "method", "class", "doctype"):
			continue
		target = node(
			entity.qualified_name.rsplit(".", 2)[-1]
			if "." in entity.qualified_name
			else entity.qualified_name,
			":::changed",
		)
		pending = {link.path + str(link.line) for link in entity.not_updated}
		for link in entity.callers[:8]:
			caller = (
				link.entity.qualified_name.rsplit(".", 1)[-1]
				if link.entity
				else f"{link.path.rsplit('/', 1)[-1]}:{link.line}"
			)
			edge = (
				"-.->|may break|"
				if (link.path + str(link.line)) in pending and entity.contract_changed
				else "-->"
			)
			lines.append(f"  {node(caller)} {edge} {target}")
	if len(lines) == 1:
		return ""
	lines.append("  classDef changed fill:#fff3cd,stroke:#d39e00")
	return "\n".join(lines)
