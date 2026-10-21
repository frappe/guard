"""Server side of the `guard` CLI."""

from pathlib import Path
from typing import Any

import frappe
from frappe import _
from werkzeug.wrappers import Response

from guard.config.resolver import ConfigResolver
from guard.git.repository import store_for
from guard.review.jobs import enqueue_review
from guard.utils import db

CLI_FILE = Path(__file__).resolve().parent.parent / "cli" / "guard_cli.py"
STATUS_CODES = {"Completed": 0, "Queued": 3, "Running": 3, "Failed": 4, "Cancelled": 5, "Skipped": 0}
MAX_PAYLOAD = 20 * 1024 * 1024


@frappe.whitelist(allow_guest=True, methods=["GET"])
def download() -> Response:
	text = CLI_FILE.read_text().replace('DEFAULT_SITE = ""', f'DEFAULT_SITE = "{frappe.utils.get_url()}"', 1)
	return Response(
		text, mimetype="text/x-python", headers={"Content-Disposition": "attachment; filename=guard"}
	)


@frappe.whitelist(methods=["POST"])
def review(payload: dict[str, Any], tier: str = "", instructions: str = "") -> dict[str, Any]:
	"""Create a CLI review. The payload has the diff and file contents of the branch."""
	if len(frappe.as_json(payload)) > MAX_PAYLOAD:
		frappe.throw(_("The review is too large for one request."))
	repository = (
		db.value("Code Repository", {"full_name": payload.get("full_name")}, "name")
		if payload.get("full_name")
		else None
	)
	if repository:
		frappe.has_permission("Code Repository", "read", str(repository), throw=True)
	doc = frappe.get_doc(
		{
			"doctype": "Code Review",
			"source": "cli",
			"status": "Queued",
			"trigger": "cli",
			"repository": repository,
			"requested_tier": tier if tier in ("plus", "apex") else "base",
			"instructions": instructions,
			"head_sha": payload.get("head_sha"),
			"branch": payload.get("branch"),
			"cli_payload": frappe.as_json(payload),
			"triggered_by_user": frappe.session.user,
		}
	).insert()
	enqueue_review(str(doc.name), tier=str(doc.get("requested_tier") or ""))
	return {"id": doc.name, "status": "Queued"}


@frappe.whitelist(methods=["GET"])
def show(review_id: str) -> dict[str, Any]:
	doc = frappe.get_doc("Code Review", review_id)
	doc.check_permission("read")
	findings = frappe.get_all(
		"Review Finding",
		filters={"code_review": review_id, "status": ["in", ["Open", "Addressed"]]},
		fields=[
			"name",
			"path",
			"start_line",
			"end_line",
			"severity",
			"comment_type",
			"title",
			"body",
			"suggestion",
			"fix_prompt",
			"outside_diff",
		],
		order_by="severity asc, path asc, start_line asc",
	)
	return {
		"id": doc.name,
		"status": doc.get("status"),
		"stage": doc.get("stage"),
		"tier": doc.get("tier"),
		"head_sha": doc.get("head_sha"),
		"summary": doc.get("summary"),
		"confidence_score": doc.get("confidence_score"),
		"risk_level": doc.get("risk_level"),
		"error": doc.get("error"),
		"findings": [dict(f) for f in findings],
	}


@frappe.whitelist(methods=["GET"])
def recent(limit: int = 10, full_name: str = "") -> list[dict[str, Any]]:
	filters: dict[str, Any] = {"triggered_by_user": frappe.session.user}
	if full_name:
		filters["repository"] = db.value("Code Repository", {"full_name": full_name}, "name")
	return [
		dict(r)
		for r in frappe.get_list(
			"Code Review",
			filters=filters,
			fields=["name", "status", "branch", "head_sha", "creation", "posted_count", "confidence_score"],
			limit=min(int(limit), 50),
			order_by="creation desc",
		)
	]


@frappe.whitelist(methods=["GET"])
def status(head_sha: str, full_name: str = "") -> dict[str, Any]:
	"""Latest review of a commit and the CLI exit code for it."""
	filters: dict[str, Any] = {"head_sha": head_sha}
	if full_name:
		filters["repository"] = db.value("Code Repository", {"full_name": full_name}, "name")
	rows = frappe.get_list(
		"Code Review", filters=filters, fields=["name", "status"], order_by="creation desc", limit=1
	)
	if not rows:
		return {"exit_code": 1, "status": None}
	return {"exit_code": STATUS_CODES.get(rows[0].status, 1), "status": rows[0].status, "id": rows[0].name}


@frappe.whitelist(methods=["GET"])
def config(full_name: str, path: str = "", sha: str = "") -> dict[str, Any]:
	"""The effective config for a path, with the source of each value."""
	repository = db.value("Code Repository", {"full_name": full_name}, "name")
	if not repository:
		frappe.throw(_("Unknown repository {0}").format(full_name))
	frappe.has_permission("Code Repository", "read", str(repository), throw=True)
	store = store_for(str(repository))
	ref = sha or store.rev_parse(
		f"refs/heads/{db.value('Code Repository', str(repository), 'default_branch')}"
	)
	return ConfigResolver.from_store(str(repository), store, ref).explain(path)


@frappe.whitelist(methods=["GET"])
def fix_prompt(target: str) -> dict[str, Any]:
	"""The fix prompt of one finding, or of every open finding of a pull request."""
	if frappe.db.exists("Review Finding", target):
		doc = frappe.get_doc("Review Finding", target)
		doc.check_permission("read")
		repository = str(doc.get("repository") or "")
		prompts = [str(doc.get("fix_prompt") or "")]
	else:
		frappe.has_permission("Pull Request", "read", target, throw=True)
		repository = str(db.value("Pull Request", target, "repository") or "")
		prompts = [
			str(r[0] or "")
			for r in db.rows(
				"SELECT fix_prompt FROM `tabReview Finding` WHERE pull_request = %s AND status = 'Open' AND posted = 1 ORDER BY path, start_line",
				(target,),
			)
		]
	full_name = db.value("Code Repository", repository, "full_name") if repository else ""
	text = "\n\n---\n\n".join(p for p in prompts if p)
	return {
		"repository": full_name,
		"prompt": f"Fix these code review findings in {full_name}.\n\n{text}" if len(prompts) > 1 else text,
	}


@frappe.whitelist(methods=["GET"])
def whoami() -> dict[str, Any]:
	user = frappe.session.user
	return {
		"user": user,
		"name": frappe.utils.get_fullname(user),
		"roles": [r for r in frappe.get_roles(user) if r.startswith("Guard")],
		"site": frappe.utils.get_url(),
	}


@frappe.whitelist(methods=["POST"])
def issue_key() -> dict[str, str]:
	"""A new API key for the signed-in user, for `guard login`. The secret is shown once."""
	if frappe.session.user == "Guest":
		frappe.throw(_("Sign in first."), frappe.PermissionError)
	from frappe.core.doctype.user.user import generate_keys

	keys = generate_keys(str(frappe.session.user))
	api_key = frappe.db.get_value("User", frappe.session.user, "api_key")
	return {"key": str(api_key), "secret": str(keys["api_secret"])}
