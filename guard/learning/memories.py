"""Learned memories: Custom Context of type Pattern. See docs/spec/08-learning.md."""

from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _

from guard.config.dashboard import active_context
from guard.config.globs import match_path
from guard.utils import db

if TYPE_CHECKING:
	from guard.guard.doctype.custom_context.custom_context import CustomContext

PREFIX = "memory:"
RETIRE_MIN_REJECTED = 3


def for_paths(
	full_name: str | None, team: str | None, paths: list[str], limit: int = 20
) -> list[dict[str, Any]]:
	"""Active memories that apply to the repository and to at least one path."""
	rows = active_context(full_name, team, types=("Pattern",))
	chosen = []
	for row in rows:
		scope = [line.strip() for line in (row.get("path_scope") or "").splitlines() if line.strip()]
		if not scope or any(match_path(path, scope) for path in paths):
			chosen.append(row)
	return chosen[-limit:]


def note(memory: dict[str, Any]) -> str:
	"""A memory as review context, with the rule id a finding cites when it acts on it."""
	title, body = memory.get("title") or "Memory", memory.get("body") or ""
	text = body if body.startswith(title) else f"{title}: {body}"
	return f"Team memory (rule_id: {PREFIX}{memory['name']}): {text}"


def cited(rule_id: str | None) -> str | None:
	"""The Custom Context of a memory that a finding cites."""
	name = (rule_id or "").removeprefix(PREFIX)
	if not (rule_id or "").startswith(PREFIX) or not frappe.db.exists("Custom Context", name):
		return None
	return name


def evidence(context: str) -> list[dict[str, Any]]:
	"""Each evidence entry with its author, text and pull request, oldest first."""
	names = frappe.parse_json(db.value("Custom Context", context, "evidence") or "[]") or []
	return [_entry(str(name)) for name in names]


def retire_unhelpful() -> list[str]:
	"""Daily. Retire learned memories whose findings the team keeps rejecting."""
	retired = []
	for row in db.dict_rows(
		"""SELECT c.name, COUNT(*) AS cited,
			SUM(f.status != 'Addressed' AND (f.status = 'Ignored' OR f.downvotes > f.upvotes)) AS rejected,
			SUM(f.status = 'Addressed' OR f.upvotes > f.downvotes) AS accepted
		FROM `tabCustom Context` c JOIN `tabReview Finding` f ON f.custom_context = c.name
		WHERE c.context_type = 'Pattern' AND c.source = 'Learned' AND c.status = 'Active'
			AND f.posted = 1 AND (c.active_since IS NULL OR f.creation > c.active_since)
		GROUP BY c.name"""
	):
		rejected, accepted = int(row["rejected"] or 0), int(row["accepted"] or 0)
		if rejected >= RETIRE_MIN_REJECTED and rejected > accepted:
			reason = _("Retired by Guard: the team rejected {0} of the {1} findings that cited it.")
			doc = cast("CustomContext", frappe.get_doc("Custom Context", row["name"]))
			doc.retire(reason.format(rejected, row["cited"]))
			retired.append(str(row["name"]))
	return retired


def _entry(name: str) -> dict[str, Any]:
	feedback = db.values(
		"Review Feedback",
		name,
		("author", "kind", "sentiment", "body", "pull_request", "finding", "creation"),
	)
	if feedback:
		finding = (
			db.value("Review Finding", feedback["finding"], "title") if feedback.get("finding") else None
		)
		return {"name": name, **feedback, "finding_title": finding, **_pull_request(feedback["pull_request"])}
	comment = db.values(
		"Pull Request Comment", name, ("author", "body", "pull_request", "path", "commented_at")
	)
	if comment:
		return {"name": name, "kind": "Comment", **comment, **_pull_request(comment["pull_request"])}
	return {"name": name, "missing": True}


def _pull_request(name: str | None) -> dict[str, Any]:
	row = db.values("Pull Request", name, ("number", "title", "web_url")) if name else None
	return (
		{
			"pull_request_number": row["number"],
			"pull_request_title": row["title"],
			"pull_request_url": row["web_url"],
		}
		if row
		else {}
	)
