"""Whitelisted methods for Desk pages and forms."""

from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _

from guard import settings as guard_settings
from guard.config.resolver import ConfigResolver
from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.learning import memories
from guard.llm.client import LLMClient
from guard.review import links
from guard.utils import db

if TYPE_CHECKING:
	from guard.guard.doctype.custom_context.custom_context import CustomContext

MAX_DIFF_FILES = 300
MAX_BULK = 100  # reviews one bulk action can retry


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
def cancel_review(review: str) -> str:
	"""Cancel the review now: its status, the check on the provider and Desk all show it at once.

	Return the new status.
	"""
	frappe.has_permission("Code Review", "write", review, throw=True)
	from guard.review import state

	user = str(frappe.session.user)
	try:
		state.cancel(review, state.cancel_reason(user), by=user)
	except state.NotActive:
		if db.value("Code Review", review, "status") in state.ACTIVE:
			frappe.throw(_("Guard is posting this review now. It is too late to cancel it."))
		frappe.throw(_("This review has already ended."))
	return "Cancelled"


@frappe.whitelist(methods=["POST"])
def retry_review(review: str) -> str | None:
	row = db.values("Code Review", review, ("pull_request", "source")) or {}
	# A local review links its pull request too, but only the agent of the developer can run it again.
	if not row.get("pull_request") or row.get("source") == "local":
		frappe.throw(_("Only pull request reviews can be retried here."))
	pull_request = row["pull_request"]
	return rereview(str(pull_request))


@frappe.whitelist(methods=["POST"])
def retry_reviews(reviews: str | list[str]) -> list[str]:
	"""Review again the pull requests of these reviews that did not finish.

	One new review for each pull request, at its current head. Returns the new reviews.
	"""
	names = frappe.parse_json(reviews) if isinstance(reviews, str) else reviews
	if not isinstance(names, list) or len(names) > MAX_BULK:
		frappe.throw(_("Select at most {0} reviews.").format(MAX_BULK))
	rows = frappe.get_all(
		"Code Review",
		filters={
			"name": ["in", [str(n) for n in names]],
			"status": ["in", ["Failed", "Cancelled", "Skipped"]],
			"pull_request": ["is", "set"],
		},
		fields=["pull_request"],
		order_by="creation asc",
	)
	pull_requests = dict.fromkeys(str(row.pull_request) for row in rows)
	frappe.flags.guard_provider_in_job = True
	try:
		return [review for pr in pull_requests if (review := rereview(pr))]
	finally:
		frappe.flags.guard_provider_in_job = False


@frappe.whitelist(methods=["POST"])
def set_finding_status(finding: str, status: str) -> None:
	if status not in ("Open", "Dismissed"):
		frappe.throw(_("Only Open and Dismissed can be set by hand."))
	doc = frappe.get_doc("Review Finding", finding)
	doc.check_permission("write")
	doc.db_set({"status": status, "status_reason": f"set by {frappe.session.user}"})
	from guard.review import state

	state.finding_changed(finding)


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
	"""The effective config for a path on the default branch, with the source of each value."""
	frappe.has_permission("Code Repository", "read", repository, throw=True)
	store = store_for(repository)
	ref = store.rev_parse(f"refs/heads/{db.value('Code Repository', repository, 'default_branch')}")
	return ConfigResolver.from_store(repository, store, ref).explain(path)


@frappe.whitelist(methods=["GET", "POST"])
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
		if guard_settings.offline():
			frappe.throw(_("Guard is in offline mode. Turn it off in Guard Settings first."))
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
	doc = cast("CustomContext", frappe.get_doc("Custom Context", context))
	doc.check_permission("write")
	doc.set("status", status)
	doc.save()


@frappe.whitelist()
def get_context_evidence(context: str) -> list[dict]:
	"""The comments and feedback behind a memory or a suggested rule."""
	frappe.get_doc("Custom Context", context).check_permission("read")
	return memories.evidence(context)
