"""Auto-approve low-risk pull requests after a clean 5/5 review. See docs/spec/08-learning.md."""

from typing import TYPE_CHECKING, cast

import frappe

from guard.config import gate
from guard.config.resolver import ConfigResolver
from guard.git.repository import store_for
from guard.providers import adapter_for_repository
from guard.providers.base import ProviderError
from guard.review.publish import open_finding_count
from guard.utils import db

if TYPE_CHECKING:
	from guard.guard.doctype.code_review.code_review import CodeReview
	from guard.guard.doctype.pull_request.pull_request import PullRequest

RISKS = ("low", "medium", "high", "critical")
BLOCKING_LABELS = {"do-not-merge", "manual-review"}


def maybe_approve(review_name: str) -> bool:
	review = cast("CodeReview", frappe.get_doc("Code Review", review_name))
	if review.status != "Completed" or not review.pull_request:
		return False
	pr = cast("PullRequest", frappe.get_doc("Pull Request", str(review.pull_request)))
	ok, _reason = check(review, pr)
	if not ok:
		return False
	adapter = adapter_for_repository(str(pr.repository))
	if not adapter:
		return False
	full_name = str(db.value("Code Repository", str(pr.repository), "full_name"))
	try:
		approval = adapter.approve(full_name, int(pr.number), str(review.head_sha))
	except ProviderError as error:
		frappe.log_error(str(error), "Guard auto-approve")
		return False
	pr.db_set({"guard_approved": 1, "approval_id": approval or ""})
	return True


def check(review: "CodeReview", pr: "PullRequest") -> tuple[bool, str]:
	"""Every condition of the spec. Returns (ok, first failed reason)."""
	if pr.state != "open" or pr.is_draft:
		return False, "not an open, ready pull request"
	if pr.head_sha != review.head_sha:
		return False, "new commits since the review started"
	labels = {str(label).lower() for label in frappe.parse_json(pr.labels or "[]") or []}
	if labels & BLOCKING_LABELS:
		return False, "a label blocks approval"
	if review.confidence_score != 5 or review.posted_count:
		return False, "the review is not a clean 5/5"
	if open_finding_count(str(pr.name)):
		return False, "an earlier finding is still open"
	snapshot = frappe.parse_json(review.config_snapshot or "{}") or {}
	if not ((snapshot.get("sections") or {}).get("confidence") or {}).get("included", True):
		return False, "the confidence section is off"
	store = store_for(str(pr.repository))
	paths = [c["path"] for c in frappe.parse_json(review.changed_files or "[]") or []]
	# Policy from the base branch, so a pull request cannot loosen it for itself.
	policy = ConfigResolver.from_store(
		str(pr.repository), store, str(review.merge_base_sha)
	).auto_approve_policy(paths)
	if not policy.get("enabled"):
		return False, "auto-approve is off"
	ceiling = policy.get("risk_ceiling", "low")
	if RISKS.index(review.risk_level or "critical") > RISKS.index(ceiling):
		return False, f"risk {review.risk_level} is above the ceiling {ceiling}"
	facts = gate.PullRequestFacts(
		repository=str(db.value("Code Repository", str(pr.repository), "full_name")),
		title=pr.title or "",
		description=pr.description or "",
		author=pr.author or "",
		base_branch=pr.base_branch or "",
		head_branch=pr.head_branch or "",
		labels=list(labels),
		paths=paths,
	)
	ok, reason = gate.auto_approve_filters_pass(policy.get("filters") or {}, facts)
	if not ok:
		return False, reason
	config = ConfigResolver.from_store(str(pr.repository), store, str(review.head_sha)).resolve_pr(paths)
	ok, reason = gate.filters_pass(config.settings, facts)
	return (ok, reason) if not ok else (True, "")


def dismiss(pull_request: str) -> None:
	"""New commits arrived: take back an earlier approval."""
	pr = cast("PullRequest", frappe.get_doc("Pull Request", pull_request))
	if not pr.guard_approved or not pr.approval_id:
		return
	adapter = adapter_for_repository(str(pr.repository))
	if adapter:
		try:
			adapter.dismiss_approval(
				str(db.value("Code Repository", str(pr.repository), "full_name")),
				int(pr.number),
				pr.approval_id,
			)
		except ProviderError as error:
			frappe.log_error(str(error), "Guard dismiss approval")
	pr.db_set({"guard_approved": 0, "approval_id": ""})
