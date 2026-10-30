"""Questions about indexed repositories (codebase chat and the query API)."""

from dataclasses import dataclass, field
from typing import Any

from guard.conversation import answer as answerer
from guard.git.repository import store_for
from guard.graph.queries import ChunkHit, CodeGraph
from guard.llm.embeddings import Embedder
from guard.review.prompts import data
from guard.review.run import ReviewRun
from guard.utils import db

SOURCE_LIMIT = 8


@dataclass
class Source:
	repository: str
	branch: str
	filepath: str
	linestart: int
	lineend: int
	summary: str
	distance: float

	def as_dict(self) -> dict[str, Any]:
		return self.__dict__.copy()


@dataclass
class CodebaseAnswer:
	message: str
	sources: list[Source] = field(default_factory=list)


def run_for_repository(repository: str) -> ReviewRun:
	row = db.values("Code Repository", repository, ("full_name", "default_index", "default_branch")) or {}
	index = row.get("default_index")
	sha = str(db.value("Repository Index", index, "sha") or "") if index else ""
	store = store_for(repository)
	return ReviewRun(
		review=None,
		repository=repository,
		full_name=str(row.get("full_name") or ""),
		head=lambda path: store.show(sha, path) if sha else None,
		base=lambda path: None,
		list_dir=lambda path: store.list_dir(sha, path) if sha else [],
		head_sha=sha,
		base_sha=sha,
		grep=(lambda pattern, glob: store.grep(sha, pattern, glob)) if sha else None,
		title=f"Codebase {row.get('full_name')}",
		base_branch=str(row.get("default_branch") or ""),
		graph=CodeGraph(str(index)) if index else None,
	)


def search(repositories: list[str], query: str, k: int = SOURCE_LIMIT) -> list[Source]:
	rows = [
		row
		for repository in repositories
		if (row := db.values("Code Repository", repository, ("full_name", "default_index", "default_branch")))
		and row.get("default_index")
	]
	# One embedding for all the repositories.
	vector = Embedder().embed_one(query) if rows else None
	sources: list[Source] = []
	for row in rows:
		for hit in CodeGraph(str(row["default_index"])).search(query, k, vector=vector):
			sources.append(_source(row, hit))
	sources.sort(key=lambda s: s.distance)
	return sources[:k]


def ask(repositories: list[str], messages: list[dict[str, str]], genius: bool = False) -> CodebaseAnswer:
	question = next((m.get("content") or "" for m in reversed(messages) if m.get("role") == "user"), "")
	history = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in messages[:-1])[-6000:]
	sources = search(repositories, question)
	if not repositories:
		return CodebaseAnswer("No repository is selected.")
	run = run_for_repository(repositories[0])
	extra = "Search results:\n" + "\n\n".join(
		data(f"{s.repository}:{s.filepath}:{s.linestart}", s.summary) for s in sources
	)
	if len(repositories) > 1:
		extra += "\nOther repositories in scope: " + ", ".join(
			str(db.value("Code Repository", r, "full_name")) for r in repositories[1:]
		)
	result = answerer.answer(run, question, history, extra, tier="plus" if genius else "base")
	return CodebaseAnswer(result.text, sources)


def _source(row: dict[str, Any], hit: ChunkHit) -> Source:
	lines = hit.content.splitlines()
	summary = "\n".join(lines[1:12]) if len(lines) > 1 else hit.content
	return Source(
		str(row["full_name"]),
		str(row.get("default_branch") or ""),
		hit.path,
		hit.start_line,
		hit.end_line,
		summary,
		round(1 - hit.score, 4),
	)
