"""Building the context pack for one review from the graph and its sources."""

from dataclasses import dataclass, field
from typing import Any

from guard.config.resolver import PullRequestConfig
from guard.graph.overlay import BlastRadius, CallSite, ChangedEntity
from guard.graph.queries import ChunkHit, CodeGraph, Link
from guard.integrations.tickets import Ticket
from guard.knowledge import store as kb_store
from guard.learning.memories import for_paths as memories_for_paths
from guard.review import crossrepo
from guard.review.prompt import ContextFile, ContextPack, Question, context_file
from guard.review.run import Reader
from guard.review.spans import ContextSpan, symbol_span
from guard.utils import db

MAX_CONTEXT_FILES = 40
MAX_FILE_LINES = 800
BUDGET_BY_SIZE = {1: 1200, 3: 700, 6: 500, 12: 350, 24: 250, 40: 200}
CALLER_PAD = 10


@dataclass
class SourceNote:
	kind: str  # tickets | knowledge | memory | rules | patterns
	text: str


@dataclass
class BuiltContext:
	pack: ContextPack
	spans: dict[str, list[ContextSpan]] = field(default_factory=dict)
	sources: list[SourceNote] = field(default_factory=list)


def budget_for(file_count: int) -> int:
	for threshold, budget in BUDGET_BY_SIZE.items():
		if file_count <= threshold:
			return budget
	return 150


def instructions(config: PullRequestConfig) -> list[tuple[str, str]]:
	seen: set[tuple[str, str]] = set()
	result = []
	for file_config in config.files.values():
		for source, text in file_config.instructions:
			key = (source, text)
			if key not in seen:
				seen.add(key)
				result.append((source, text))
	return result


def build(
	read: Reader,
	changed: list[ChangedEntity],
	blast: BlastRadius,
	graph: CodeGraph | None,
	config: PullRequestConfig,
	paths: list[str],
	tickets: list[Ticket] | None = None,
	full_name: str | None = None,
	team: str | None = None,
	repository: str | None = None,
	rule_files: list[tuple[str, str]] | None = None,
) -> BuiltContext:
	pack = ContextPack()
	spans: dict[str, list[ContextSpan]] = {}
	sources: list[SourceNote] = []
	budget = budget_for(len(paths))

	for source, text in instructions(config)[:10]:
		pack.files.append(ContextFile(f"instructions/{source}", text, "custom instruction"))
	for path, text in (rule_files or [])[:6]:
		pack.files.append(ContextFile(path, text, "project rule file"))
	for ticket in tickets or []:
		sources.append(SourceNote("tickets", ticket.as_text()))
	for memory in memories_for_paths(full_name, team, paths):
		sources.append(SourceNote("memory", f"{memory.get('title') or 'Memory'}: {memory.get('content')}"))

	if repository:
		for document in kb_store.documents_for_paths(repository, paths):
			sources.append(
				SourceNote(
					"knowledge",
					f"Knowledge base {document['path']}:\n{(document.get('content') or '')[:4000]}",
				)
			)
		provider = db.value("Code Repository", repository, "code_provider")
		crossrepo.ensure_indexed(crossrepo.related(repository, str(provider or ""), config.repos))
		for line in crossrepo.context_lines(changed, crossrepo.LineReader()):
			sources.append(SourceNote("crossrepo", f"Other repository: {line}"))
	for file_ref in _config_files(config):
		f = context_file(read, file_ref[0], f"team context file: {file_ref[1]}", limit=budget)
		if f:
			pack.files.append(f)
	call_lines = _call_lines(changed)
	for path, reasons in impacted_files(changed).items():
		if path in paths:
			continue
		f = context_file(read, path, f"impacted: {', '.join(reasons[:3])}", limit=MAX_FILE_LINES)
		if f:
			pack.files.append(f)
			spans[path] = [
				*spans.get(path, []),
				*(
					ContextSpan(max(n - CALLER_PAD, 1), n + CALLER_PAD, "call")
					for n in call_lines.get(path, [])
				),
			]

	if graph:
		_symbol_context(read, changed, graph, pack, spans, budget, paths)

	for path in paths:
		if not any(f.path == path for f in pack.files):
			f = context_file(read, path, "changed file", limit=min(MAX_FILE_LINES, budget + 200))
			if f:
				pack.files.append(f)
	pack.files = pack.files[:MAX_CONTEXT_FILES]
	pack.notes = [n.text for n in sources]
	return BuiltContext(pack, spans, sources)


def _config_files(config: PullRequestConfig) -> list[tuple[str, str]]:
	seen: dict[str, str] = {}
	for file_config in config.files.values():
		for ref in file_config.files:
			seen.setdefault(ref.path, ref.description or ref.source)
	return list(seen.items())[:10]


def _call_lines(changed: list[ChangedEntity]) -> dict[str, list[int]]:
	lines: dict[str, list[int]] = {}
	for entity in changed:
		for link in [*entity.callers, *(site.link for site in entity.at_risk)]:
			if link.path and link.line:
				lines.setdefault(link.path, []).append(link.line)
	return lines


def impacted_files(changed: list[ChangedEntity]) -> dict[str, list[str]]:
	"""Files outside the diff that touch changed entities, best first, with caller names as reasons."""
	score: dict[str, int] = {}
	reasons: dict[str, list[str]] = {}
	for entity in changed:
		links: list[Link] = list(entity.callers)
		sites: list[CallSite] = list(entity.at_risk)
		for link in links + [site.link for site in sites]:
			if not link.path:
				continue
			score[link.path] = score.get(link.path, 0) + 2 if link.resolved else score.get(link.path, 0) + 1
			name = link.entity.label if link.entity else f"{link.name} at {link.path}:{link.line}"
			bucket = reasons.setdefault(link.path, [])
			if name not in bucket:
				bucket.append(name)
	return {path: reasons[path] for path in sorted(score, key=lambda p: -score[p])}


def _symbol_context(
	read: Reader,
	changed: list[ChangedEntity],
	graph: CodeGraph,
	pack: ContextPack,
	spans: dict[str, list[ContextSpan]],
	budget: int,
	paths: list[str],
) -> None:
	for entity in changed[:12]:
		question_id = f"{entity.qualified_name}@{entity.path}"
		for link in entity.not_updated:
			caller = link.entity.label if link.entity else f"{link.name} ({link.path}:{link.line})"
			pack.questions.append(
				Question(
					question_id,
					f"`{caller}` calls `{entity.name}` and the call may not fit its new signature — is the call still correct?",
					"missing",
				)
			)
			_add_file(read, pack, spans, link, budget, f"caller of {entity.name}")
		for site in entity.at_risk[:4]:
			pack.questions.append(
				Question(
					question_id,
					f"Call site `{site.link.path}:{site.link.line}` calls `{entity.name}` with arguments that changed — check: {site.snippet.strip()[:160]}",
					"verify",
				)
			)
			_add_file(read, pack, spans, site.link, budget, f"call site of {entity.name}")
		if entity.siblings:
			sibling = entity.siblings[0]
			pack.questions.append(
				Question(
					f"pattern-{question_id}",
					f"`{sibling.label}` is a similar {entity.kind} in this codebase — is the change consistent with how it works?",
					"verify",
				)
			)
			_add_file(read, pack, spans, _link_to(sibling), budget, f"similar {entity.kind}")
		_pattern_chunks(read, graph, entity, pack, spans, budget)
	for entity in changed[:8]:
		for name in entity.callees[:6]:
			if not graph.definitions(name):
				pack.questions.append(
					Question(
						f"miss-{name}",
						f"`{name}` is called from `{entity.name}` but not defined in the repository — renamed, removed, or dynamic?",
						"missing",
					)
				)


def _pattern_chunks(
	read: Reader,
	graph: CodeGraph,
	entity: ChangedEntity,
	pack: ContextPack,
	spans: dict[str, list[ContextSpan]],
	budget: int,
) -> None:
	if not entity.head_body.strip():
		return
	hits = graph.similar(entity.head_body, k=3)
	for hit in hits:
		if hit.path == entity.path:
			continue
		f = context_file(read, hit.path, f"similar code to {entity.name}", limit=budget // 2)
		if f:
			pack.files.append(f)
			span = symbol_span(hit.start_line, hit.end_line)
			spans[hit.path] = [*spans.get(hit.path, []), span]


def _add_file(
	read: Reader, pack: ContextPack, spans: dict[str, list[ContextSpan]], link: Link, budget: int, reason: str
) -> None:
	if not link.path:
		return
	existing = next((f for f in pack.files if f.path == link.path), None)
	if not existing:
		f = context_file(read, link.path, reason, limit=budget)
		if not f:
			return
		pack.files.append(f)
	span = (
		symbol_span(link.entity.start_line, link.entity.end_line)
		if link.entity
		else ContextSpan(max(link.line - CALLER_PAD, 1), link.line + CALLER_PAD, reason)
	)
	spans[link.path] = [*spans.get(link.path, []), span]


def _link_to(entity: Any) -> Link:
	return Link(entity, entity.name, "", entity.path, entity.start_line, True)


def excerpt(text: str, spans: list[ContextSpan] | None) -> str:
	"""The lines inside spans, numbered, with a marker where lines are left out."""
	if not spans:
		return text
	lines = text.splitlines()
	keep: set[int] = set()
	for span in spans:
		keep.update(range(max(span.start, 1), min(span.end, len(lines)) + 1))
	if not keep:
		return text
	out, previous = [], 0
	for number in sorted(keep):
		if number != previous + 1:
			out.append("   ...")
		out.append(f"{number:5}  {lines[number - 1]}")
		previous = number
	return "\n".join(out)
