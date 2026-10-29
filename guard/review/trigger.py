"""Decide if an event starts a review, then create it."""

from typing import TYPE_CHECKING, cast

import frappe

from guard import settings as guard_settings
from guard.config import gate
from guard.config.globs import match_filter
from guard.config.resolver import ConfigResolver
from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.git.store import GitError
from guard.guard.doctype.review_settings.review_settings import get_effective
from guard.providers import adapter_for_repository
from guard.review import permissions
from guard.review.jobs import create_review
from guard.review.publish import report_skip
from guard.review.state import check_of
from guard.utils import db

PUSH_EVENTS = ("push", "rebase")

if TYPE_CHECKING:
	from guard.guard.doctype.pull_request.pull_request import PullRequest


def consider(
	pull_request: str,
	event: str,
	manual: bool = False,
	requested_by: str = "",
	requested_tier: str = "",
	instructions: str = "",
	force_draft: bool = False,
) -> str | None:
	"""Run the review gate. Return the new Code Review name, or None when it is skipped."""
	pr = cast("PullRequest", frappe.get_doc("Pull Request", pull_request))
	repository = str(pr.repository)
	repo = db.values("Code Repository", repository, ("reviews_enabled", "full_name", "team")) or {}
	if not repo.get("reviews_enabled"):
		return None
	login = requested_by or str(pr.author or "")
	if not permissions.allowed("tag" if manual else "author", login, repository):
		return None
	store = store_for(repository)
	adapter = adapter_for_repository(repository)
	number = int(pr.number or 0)
	store.fetch_branches(str(pr.base_branch or ""))
	head = store.fetch_pull(adapter.pull_ref(number), number) if adapter else str(pr.head_sha)
	base = store.rev_parse(f"refs/heads/{pr.get('base_branch')}")
	merge_base = store.merge_base(base, head)
	files = parse_diff(store.diff(merge_base, head))
	paths = [p for f in files for p in {f.path, f.old_path} if p]
	# Config from the base branch, so a pull request cannot change how Guard reviews it.
	config = ConfigResolver.from_store(repository, store, base).resolve_pr([f.path for f in files])
	facts = gate.PullRequestFacts(
		repository=str(repo.get("full_name")),
		title=str(pr.title or ""),
		description=str(pr.description or ""),
		author=str(pr.author or ""),
		base_branch=str(pr.base_branch or ""),
		head_branch=str(pr.head_branch or ""),
		is_draft=bool(pr.is_draft) and not force_draft,
		conflicts=not manual and store.conflicts(base, head),
		labels=list(frappe.parse_json(pr.labels or "[]") or []),
		paths=paths,
		additions=sum(f.additions for f in files),
		deletions=sum(f.deletions for f in files),
	)
	rules = gate.dashboard_rules(get_effective(repo.get("team")))
	result = gate.check(facts, config, event, manual, rules)
	if not result.allowed:
		frappe.logger("guard").info(f"review skipped for {pull_request}: {result.reason}")
		if adapter and config.settings.get("status_check", True) and not owns_check(pull_request, head):
			report_skip(
				adapter,
				str(repo.get("full_name")),
				head,
				result.reason,
				check_id=check_of(pull_request, head),
			)
		return None
	trigger = event if not manual else ("retrigger" if event == "retrigger" else "mention")
	user = permissions.user_for_login(requested_by) if requested_by else None
	return create_review(
		pull_request,
		trigger,
		requested_by,
		requested_tier,
		instructions,
		user,
		bool(config.settings.get("status_check", True)),
		delay=push_delay(event, manual),
	)


def push_delay(event: str, manual: bool) -> int:
	"""Seconds that a review waits before it starts. Pushes often come in a row: the review of each one
	but the last would be cancelled after it spent tokens. An opened pull request and a request do not wait.
	"""
	if manual or event not in PUSH_EVENTS:
		return 0
	return guard_settings.push_review_delay()


def owns_check(pull_request: str, head: str) -> bool:
	"""A review of the commit set a result or runs, or a queued review will review it.

	A skip must not replace the check of that review: GitHub counts a skipped check as passing. A queued
	review fetches the newest head when it starts. A running review keeps its own commit.
	"""
	return bool(
		db.scalar(
			"""SELECT COUNT(*) FROM `tabCode Review` WHERE pull_request = %s
			AND ((head_sha = %s AND status IN ('Completed', 'Failed', 'Running'))
				OR (status = 'Queued' AND cancel_requested = 0))""",
			(pull_request, head),
		)
	)


def push_kind(repository: str, before: str | None, after: str | None) -> str:
	"""`push` when the old head is an ancestor of the new head, else `rebase`."""
	if not before or not after or set(before) == {"0"}:
		return "push"
	store = store_for(repository)
	try:
		store.fetch_sha(after)
		store.fetch_sha(before)
	except Exception:
		return "rebase"
	return "push" if store.is_ancestor(before, after) else "rebase"


def trigger_label(pull_request: str, label: str) -> bool:
	"""A label in the `labels` filter counts as an `open` event."""
	pr = db.values("Pull Request", pull_request, ("repository", "base_branch")) or {}
	repository = str(pr.get("repository"))
	store = store_for(repository)
	# The mirror's base branch is enough here: `consider` fetches it again before the gate.
	try:
		base = store.rev_parse(f"refs/heads/{pr.get('base_branch')}")
	except GitError:
		return False
	config = ConfigResolver.from_store(repository, store, base).resolve("")
	return any(match_filter(label, [pattern]) for pattern in config.settings.get("labels") or [])
