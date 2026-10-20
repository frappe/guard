"""Dashboard config as layers: Review Settings, Custom Context and repository overrides."""

import json
from typing import Any

import frappe

from guard.config.globs import match_filter
from guard.config.schema import SECTIONS, FileRef, Layer, Note, Rule
from guard.utils import db

AUTO_REVIEW = {
	"Never": [],
	"On pull request opened": ["open"],
	"On new pushes": ["open", "push"],
	"On all events": ["open", "push", "rebase"],
}


def layers(repository: str | None, team: str | None = None) -> list[Layer]:
	"""Base layers for a repository, lowest priority first."""
	from guard.guard.doctype.review_settings.review_settings import get_effective

	repo = (
		db.values("Code Repository", repository, ("full_name", "team", "review_tier", "strictness"))
		if repository
		else None
	)
	team = team or (repo or {}).get("team")
	settings_doc = get_effective(team)
	result = [settings_layer(settings_doc), context_layer((repo or {}).get("full_name"), team)]
	if repo and (repo.get("review_tier") or repo.get("strictness")):
		override = Layer(source="repository override")
		if repo.get("review_tier"):
			override.settings["effort"] = repo["review_tier"]
		if repo.get("strictness"):
			override.settings["strictness"] = int(repo["strictness"])
		result.append(override)
	return result


def settings_layer(doc: Any) -> Layer:
	source = f"dashboard: {doc.name}"
	types = [name for name in ("logic", "syntax", "style") if doc.get(f"comment_{name}")]
	layer = Layer(source=source)
	layer.settings = {
		"strictness": int(doc.get("strictness") or 2),
		"comment_types": types,
		"effort": doc.get("review_tier") or "auto",
		"auto_review": AUTO_REVIEW.get(doc.get("auto_review") or "", ["open"]),
		"review_drafts": bool(doc.get("review_drafts")),
		"status_check": bool(doc.get("status_check")),
		"status_comments": bool(doc.get("status_comments")),
		"update_description": bool(doc.get("update_description")),
		"update_summary_only": bool(doc.get("update_summary_only")),
		"fix_with_ai": bool(doc.get("fix_with_ai")),
		"hide_footer": bool(doc.get("hide_footer")),
		"comments_outside_diff": bool(doc.get("comments_outside_diff")),
		"include_rule_files": bool(doc.get("include_rule_files")),
		"sections": {
			name: {
				"included": bool(doc.get(f"{name}_included")),
				"collapsible": bool(doc.get(f"{name}_collapsible")),
				"default_open": bool(doc.get(f"{name}_open")),
			}
			for name in SECTIONS
		},
	}
	layer.instructions = (doc.get("instructions") or "").strip()
	layer.ignore_patterns = [line for line in (doc.get("ignore_patterns") or "").splitlines() if line.strip()]
	if doc.get("auto_approve") or doc.get("auto_approve_filters"):
		layer.auto_approve = {
			"enabled": bool(doc.get("auto_approve")),
			"risk_ceiling": doc.get("risk_ceiling") or "low",
			"filters": _json(doc.get("auto_approve_filters")),
		}
	return layer


def context_layer(full_name: str | None, team: str | None) -> Layer:
	"""Active dashboard Custom Context that applies to this repository."""
	layer = Layer(source="dashboard: custom context")
	instructions = []
	for row in active_context(full_name, team):
		scope = [line.strip() for line in (row["path_scope"] or "").splitlines() if line.strip()]
		kind = row["context_type"]
		if kind == "Rule":
			layer.rules.append(
				Rule(
					rule=row["body"],
					source=f"dashboard rule {row['name']}",
					id=row["rule_id"] or row["name"],
					scope=scope,
					severity=row["severity"] or None,
					context_name=row["name"],
				)
			)
		elif kind == "File" and row["file_path"]:
			layer.files.append(
				FileRef(
					path=row["file_path"],
					source=f"dashboard file {row['name']}",
					description=row["body"] or "",
					scope=scope,
				)
			)
		elif kind == "Instruction":
			instructions.append(row["body"])
		elif kind == "Other":
			layer.notes.append(Note(content=row["body"], source=f"dashboard note {row['name']}", scope=scope))
	layer.instructions = "\n\n".join(i for i in instructions if i)
	return layer


def active_context(
	full_name: str | None, team: str | None, types: tuple[str, ...] = ("Rule", "File", "Instruction", "Other")
) -> list[dict[str, Any]]:
	rows = frappe.get_all(
		"Custom Context",
		filters={"status": "Active", "context_type": ["in", types]},
		fields=[
			"name",
			"context_type",
			"title",
			"body",
			"file_path",
			"severity",
			"team",
			"repository_scope",
			"path_scope",
			"rule_id",
		],
		order_by="creation asc",
	)
	return [row for row in rows if _applies(row, full_name, team)]


def _applies(row: dict[str, Any], full_name: str | None, team: str | None) -> bool:
	if row.get("team") and row["team"] != team:
		return False
	scopes = [line.strip() for line in (row.get("repository_scope") or "").splitlines() if line.strip()]
	return not scopes or (bool(full_name) and match_filter(full_name or "", scopes))


def _json(value: Any) -> dict[str, Any]:
	if isinstance(value, dict):
		return value
	try:
		data = json.loads(value or "{}")
	except TypeError, json.JSONDecodeError:
		return {}
	return data if isinstance(data, dict) else {}
