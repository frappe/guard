"""Whitelisted methods for Desk pages and forms."""

from typing import Any

import frappe
from frappe import _

from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.llm.client import LLMClient
from guard.review import links
from guard.utils import db

MAX_DIFF_FILES = 300


@frappe.whitelist(methods=["GET", "POST"])
def pull_requests(state: str = "open", search: str = "", limit: int = 100) -> list[dict[str, Any]]:
	filters: dict[str, Any] = {"state": state} if state in ("open", "closed", "merged") else {}
	if state == "closed":
		filters = {"state": ["in", ["closed", "merged"]]}
	or_filters = {"title": ["like", f"%{search}%"], "author": ["like", f"%{search}%"]} if search else None
	return [
		dict(r)
		for r in frappe.get_list(
			"Pull Request",
			filters=filters,
			or_filters=or_filters,
			fields=[
				"name",
				"number",
				"title",
				"author",
				"repository",
				"state",
				"is_draft",
				"confidence_score",
				"risk_level",
				"review_count",
				"modified",
			],
			order_by="modified desc",
			limit=min(int(limit), 300),
		)
	]


@frappe.whitelist(methods=["GET", "POST"])
def pull_request_view(pull_request: str) -> dict[str, Any]:
	"""Everything the review page shows: overview, findings, tour, diff."""
	pr = frappe.get_doc("Pull Request", pull_request)
	pr.check_permission("read")
	review = frappe.get_doc("Code Review", str(pr.get("latest_review"))) if pr.get("latest_review") else None
	findings = frappe.get_list(
		"Review Finding",
		filters={
			"pull_request": pull_request,
			"status": ["in", ["Open", "Addressed", "Dismissed", "Outdated", "Ignored"]],
			"posted": 1,
		},
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
			"status",
			"comment_url",
			"upvotes",
			"downvotes",
			"outside_diff",
		],
		order_by="severity asc, path asc, start_line asc",
	)
	return {
		"pull_request": pr.as_dict(convert_dates_to_str=True),
		"repository": db.value("Code Repository", str(pr.get("repository")), "full_name"),
		"review": review.as_dict(convert_dates_to_str=True) if review else None,
		"reviews": [
			dict(r)
			for r in frappe.get_list(
				"Code Review",
				filters={"pull_request": pull_request},
				fields=["name", "status", "tier", "trigger", "confidence_score", "posted_count", "creation"],
				order_by="creation desc",
				limit=20,
			)
		],
		"findings": [dict(f) for f in findings],
		"tour": frappe.parse_json(pr.get("tour") or "[]") or [],
		"files": diff_files(pull_request),
		"retrigger_url": links.retrigger(pull_request),
	}


def diff_files(pull_request: str) -> list[dict[str, Any]]:
	row = (
		db.values("Pull Request", pull_request, ("repository", "head_sha", "base_branch", "latest_review"))
		or {}
	)
	review = (
		db.values("Code Review", row["latest_review"], ("merge_base_sha", "head_sha"))
		if row.get("latest_review")
		else None
	)
	store = store_for(str(row["repository"]))
	try:
		head = (review or {}).get("head_sha") or row["head_sha"]
		base = (review or {}).get("merge_base_sha") or store.merge_base(
			store.rev_parse(f"refs/heads/{row['base_branch']}"), head
		)
		files = parse_diff(store.diff(base, head))
	except Exception:
		return []
	return [
		{
			"path": f.path,
			"old_path": f.old_path,
			"status": f.status,
			"additions": f.additions,
			"deletions": f.deletions,
			"hunks": [
				{
					"id": h.id,
					"header": h.header,
					"lines": [[line.kind, line.old_no, line.new_no, line.text] for line in h.lines],
				}
				for h in f.hunks
			],
		}
		for f in files[:MAX_DIFF_FILES]
	]


@frappe.whitelist(methods=["POST"])
def rereview(pull_request: str, tier: str = "") -> str | None:
	frappe.has_permission("Pull Request", "read", pull_request, throw=True)
	frappe.only_for(["Guard Admin", "Guard Member", "System Manager"])
	from guard.review.jobs import create_review

	return create_review(pull_request, "manual", requested_tier=tier, user=frappe.session.user)


@frappe.whitelist(methods=["POST"])
def cancel_review(review: str) -> None:
	frappe.has_permission("Code Review", "write", review, throw=True)
	frappe.db.set_value("Code Review", review, "cancel_requested", 1)


@frappe.whitelist(methods=["POST"])
def retry_review(review: str) -> str | None:
	pull_request = db.value("Code Review", review, "pull_request")
	if not pull_request:
		frappe.throw(_("Only pull request reviews can be retried here."))
	return rereview(str(pull_request))


@frappe.whitelist(methods=["POST"])
def set_finding_status(finding: str, status: str) -> None:
	if status not in ("Open", "Dismissed"):
		frappe.throw(_("Only Open and Dismissed can be set by hand."))
	doc = frappe.get_doc("Review Finding", finding)
	doc.check_permission("write")
	doc.db_set({"status": status, "status_reason": f"set by {frappe.session.user}"})


@frappe.whitelist(methods=["POST"])
def index_now(repository: str, full: bool = False) -> str:
	frappe.has_permission("Code Repository", "write", repository, throw=True)
	from guard.indexing.jobs import enqueue_index

	return enqueue_index(repository, full=bool(full))


@frappe.whitelist(methods=["POST"])
def build_knowledge_base(repository: str) -> None:
	frappe.has_permission("Code Repository", "write", repository, throw=True)
	from guard.knowledge.builder import enqueue_build

	enqueue_build(repository)


@frappe.whitelist(methods=["GET", "POST"])
def explain_config(repository: str, path: str = "") -> dict[str, Any]:
	frappe.has_permission("Code Repository", "read", repository, throw=True)
	from guard.api.cli import config

	return config(str(db.value("Code Repository", repository, "full_name")), path)


@frappe.whitelist(methods=["POST"])
def sync_repositories(provider: str) -> int:
	frappe.only_for(["Guard Admin", "System Manager"])
	from guard.providers.base import ProviderError
	from guard.providers.sync import discover_installations, sync_team

	discover_installations(provider)
	count = 0
	for team in frappe.get_all("Guard Team", filters={"code_provider": provider}, pluck="name"):
		try:
			count += sync_team(team)
		except ProviderError as error:
			# For example, the App was uninstalled from this account. Sync the others.
			frappe.log_error(str(error), f"Guard sync {team}")
	return count


@frappe.whitelist(methods=["POST"])
def test_provider(provider: str) -> dict[str, Any]:
	frappe.only_for(["Guard Admin", "System Manager"])
	from guard.providers import adapter_for_provider
	from guard.providers.base import ProviderError

	team = frappe.get_all("Guard Team", filters={"code_provider": provider}, pluck="name", limit=1)
	adapter = adapter_for_provider(provider, team[0] if team else None)
	app_without_team = frappe.db.get_value("Code Provider", provider, "auth_mode") == "App" and not team
	try:
		data = adapter.request_as_app("GET", "/app") if app_without_team else adapter.request("GET", "/user")  # type: ignore[attr-defined]
	except ProviderError as error:
		return {"ok": False, "message": str(error)}
	return {
		"ok": True,
		"message": _("Connected as {0}").format(data.get("login") or data.get("slug") or data.get("name")),
	}


@frappe.whitelist(methods=["GET", "POST"])
def webhook_url(provider: str) -> str:
	from guard.webhooks.endpoint import webhook_url as url

	return url(provider)


@frappe.whitelist(methods=["POST"])
def improve_rule(text: str) -> str:
	"""Rewrite a vague rule into a specific, checkable one."""
	client = LLMClient.fast(purpose="improve_rule")
	if not client.online:
		frappe.throw(_("Set up the AI gateway in Guard Settings first."))
	return client.complete(
		"Rewrite a code review rule so a reviewer can check it on one change. Make it specific and measurable. Keep the intent. Answer with the rule only.",
		text,
		max_tokens=400,
	).strip()


@frappe.whitelist(methods=["POST"])
def set_cluster_status(cluster: str, status: str) -> None:
	if status not in ("Active", "Discarded"):
		frappe.throw(_("Use Active or Discarded."))
	frappe.get_doc("Repository Cluster", cluster).check_permission("write")
	frappe.db.set_value("Repository Cluster", cluster, "status", status)


@frappe.whitelist(methods=["POST"])
def set_context_status(context: str, status: str) -> None:
	if status not in ("Active", "Inactive", "Ignored"):
		frappe.throw(_("Use Active, Inactive or Ignored."))
	frappe.get_doc("Custom Context", context).check_permission("write")
	frappe.db.set_value("Custom Context", context, "status", status)
