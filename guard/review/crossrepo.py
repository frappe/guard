"""Cross-repo context: `context.repos` and repository clusters."""

from dataclasses import dataclass

import frappe

from guard.graph.queries import CodeGraph
from guard.utils import db


@dataclass
class OtherRepository:
	full_name: str
	repository: str
	index: str


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


def definitions_elsewhere(others: list[OtherRepository], names: list[str], limit: int = 12) -> list[str]:
	"""Lines that show where other repositories define or call the changed names."""
	lines: list[str] = []
	for other in others:
		graph = CodeGraph(other.index)
		for name in names:
			for entity in graph.definitions(name, limit=3):
				callers = graph.callers(entity, limit=5)
				lines.append(f"{other.full_name}: {entity.label} — {len(callers)} callers")
				if len(lines) >= limit:
					return lines
	return lines


def ensure_indexed(others: list[OtherRepository]) -> None:
	from guard.indexing.jobs import enqueue_index

	for other in others:
		if db.value("Repository Index", other.index, "status") != "completed":
			enqueue_index(other.repository)
