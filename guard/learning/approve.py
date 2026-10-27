"""Auto-approve low-risk pull requests after a clean 5/5 review. See docs/spec/08-learning.md."""

from typing import TYPE_CHECKING, cast

import frappe

from guard.config import gate
from guard.config.resolver import ConfigResolver
from guard.git.repository import store_for
from guard.providers import adapter_for_repository
from guard.providers.base import ProviderError
from guard.review import render, state
from guard.review.publish import open_finding_count
from guard.utils import db
from guard.utils.redact import redact

if TYPE_CHECKING:
	from guard.guard.doctype.code_review.code_review import CodeReview
	from guard.guard.doctype.pull_request.pull_request import PullRequest

RISKS = ("low", "medium", "high", "critical")
BLOCKING_LABELS = {"do-not-merge", "manual-review"}
APPROVED = "Approved"
CLOSED = "the pull request is closed"
NEW_COMMITS = "new commits since the review started"
NOT_CLEAN = "the review is not a clean 5/5"
STILL_OPEN = "an earlier finding is still open"
POLICY_OFF = "auto-approve is off"
# `check` tests these first. The summary does not repeat them: nobody waits for an approval of a
# closed pull request, a newer review follows new commits, the headline shows the findings, and a
# repository without the policy does not want to hear about it. Every later reason is shown.
QUIET_REASONS = {CLOSED, NEW_COMMITS, NOT_CLEAN, STILL_OPEN, POLICY_OFF}
NOTE = "Not auto-approved: "


def maybe_approve(review_name: str) -> bool:
	"""Approve the pull request when the review allows it. Keep the result on the review, and tell in
	the summary why a clean review did not approve: else nobody can see it."""
	review = cast("CodeReview", frappe.get_doc("Code Review", review_name))
	if review.status != "Completed" or not review.pull_request:
		return False
	pr = cast("PullRequest", frappe.get_doc("Pull Request", str(review.pull_request)))
	ok, reason = check(review, pr)
	if ok:
		ok, reason = _approve(review, pr)
	review.db_set("approval", APPROVED if ok else f"Not approved: {reason}", update_modified=False)
	if not ok and reason not in QUIET_REASONS:
		_note_in_summary(review, pr, NOTE + reason)
	return ok


def _approve(review: "CodeReview", pr: "PullRequest") -> tuple[bool, str]:
	adapter = adapter_for_repository(str(pr.repository))
	if not adapter:
		return False, "Guard cannot reach the code provider"
	full_name = str(db.value("Code Repository", str(pr.repository), "full_name"))
	try:
		approval = adapter.approve(full_name, int(pr.number), str(review.head_sha))
	except ProviderError as error:
		frappe.log_error(redact(str(error)), "Guard auto-approve")
		return False, "the code provider did not accept the approval"
	pr.db_set({"guard_approved": 1, "approval_id": approval or ""})
	return True, ""


def _note_in_summary(review: "CodeReview", pr: "PullRequest", note: str) -> None:
	"""Add the note to the footer of the summary comment, when this review wrote it last.

	The note rewrites the whole comment from the body of this review. A newer review that is queued
	or running can publish its summary before it becomes the latest review, so skip then. A newer
	review that starts after this check can still lose its summary to this edit until it publishes
	again: GitHub cannot edit a comment only when it is unchanged.
	"""
	snapshot = frappe.parse_json(review.config_snapshot or "{}") or {}
	if snapshot.get("update_description") or not (pr.summary_comment_id and review.body):
		return
	if NOTE in str(review.body):
		return
	if _newer_review(review, str(pr.name)):
		return
	adapter = adapter_for_repository(str(pr.repository))
	if not adapter:
		return
	body = render.footer_note(str(review.body), note)
	full_name = str(db.value("Code Repository", str(pr.repository), "full_name"))
	try:
		adapter.update_comment(full_name, int(pr.number), str(pr.summary_comment_id), body)
	except ProviderError as error:
		frappe.log_error(redact(str(error)), "Guard auto-approve note")
		return
	review.db_set("body", body, update_modified=False)


def _newer_review(review: "CodeReview", pull_request: str) -> bool:
	"""Another review of the pull request owns the summary, or is about to write it."""
	if str(db.value("Pull Request", pull_request, "latest_review") or "") != str(review.name):
		return True
	return bool(
		db.scalar(
			"SELECT COUNT(*) FROM `tabCode Review` WHERE pull_request = %s AND name != %s AND status IN %s",
			(pull_request, str(review.name), state.ACTIVE),
		)
	)


def check(review: "CodeReview", pr: "PullRequest") -> tuple[bool, str]:
	"""Every condition of the spec. Returns (ok, first failed reason).

	The reasons in QUIET_REASONS come first, so any later reason is about a clean review with the
	policy on: the summary shows it.
	"""
	if pr.state != "open":
		return False, CLOSED
	if pr.head_sha != review.head_sha:
		return False, NEW_COMMITS
	if review.confidence_score != 5 or review.posted_count:
		return False, NOT_CLEAN
	if open_finding_count(str(pr.name)):
		return False, STILL_OPEN
	store = store_for(str(pr.repository))
	paths = [c["path"] for c in frappe.parse_json(review.changed_files or "[]") or []]
	# Policy from the base branch, so a pull request cannot loosen it for itself.
	policy = ConfigResolver.from_store(
		str(pr.repository), store, str(review.merge_base_sha)
	).auto_approve_policy(paths)
	if not policy.get("enabled"):
		return False, POLICY_OFF
	if pr.is_draft:
		return False, "the pull request is a draft"
	labels = {str(label).lower() for label in frappe.parse_json(pr.labels or "[]") or []}
	if labels & BLOCKING_LABELS:
		return False, "a label blocks approval"
	snapshot = frappe.parse_json(review.config_snapshot or "{}") or {}
	if not ((snapshot.get("sections") or {}).get("confidence") or {}).get("included", True):
		return False, "the confidence section is off"
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
