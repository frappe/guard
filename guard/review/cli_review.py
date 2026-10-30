"""Payload reviews (`source = cli`): the diff and file contents come in `cli_payload`, not from a
mirror. Evals replay recorded pull requests this way."""

from typing import TYPE_CHECKING, Any

import frappe
from frappe.utils import now_datetime

from guard.config.resolver import ConfigResolver, is_config_path
from guard.git.diff import parse_diff
from guard.git.store import Commit
from guard.indexing import frappe_plugin
from guard.review.run import ReviewRun

if TYPE_CHECKING:
	from guard.guard.doctype.code_review.code_review import CodeReview

MAX_FILES = 300


def prepare_cli(review: "CodeReview") -> ReviewRun | None:
	payload: dict[str, Any] = frappe.parse_json(review.cli_payload or "{}") or {}
	head_files: dict[str, str] = payload.get("files") or {}
	base_files: dict[str, str] = payload.get("base_files") or {}
	tree: list[str] = payload.get("tree") or list(head_files)
	files = parse_diff(payload.get("diff") or "")
	repository = review.repository or None
	config = ConfigResolver.from_files(
		repository, {p: t for p, t in head_files.items() if is_config_path(p)}
	).resolve_pr([f.path for f in files])
	reviewed = set(config.reviewed_paths())
	run = ReviewRun(
		review=review,
		repository=repository,
		full_name=str(payload.get("full_name") or ""),
		head=head_files.get,
		base=base_files.get,
		list_dir=lambda path: _list(tree, path),
		head_sha=str(payload.get("head_sha") or ""),
		base_sha=str(payload.get("base_sha") or ""),
		title=str(payload.get("title") or f"Branch {payload.get('branch') or ''}"),
		description=str(payload.get("description") or ""),
		author=str(payload.get("author") or frappe.session.user),
		base_branch=str(payload.get("base_branch") or "main"),
		head_branch=str(payload.get("branch") or ""),
		all_files=files,
		files=[f for f in files if f.path in reviewed and not f.is_binary][:MAX_FILES],
		config=config,
		commits=[
			Commit(c.get("sha", ""), "", c.get("subject", ""), "", "") for c in payload.get("commits") or []
		],
		instructions=review.instructions or "",
		frappe_app=frappe_plugin.is_frappe_app(set(tree)),
	)
	run.notes = list(config.errors)
	if not run.files:
		review.db_set({"status": "Skipped", "error": "no reviewable files", "completed_at": now_datetime()})
		return None
	return run


def _list(tree: list[str], path: str) -> list[tuple[str, str]]:
	prefix = f"{path.strip('/')}/" if path.strip("/") else ""
	entries: dict[str, str] = {}
	for item in tree:
		if not item.startswith(prefix):
			continue
		rest = item[len(prefix) :]
		name, _, more = rest.partition("/")
		entries[name] = "tree" if more else "blob"
	return sorted(entries.items())
