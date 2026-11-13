"""Post a submitted local review on the pull request of its branch. See docs/spec/05-review-engine.md
(Local reviews, Publish).

The developer paid for the model, so the post takes no review slot and no credits. Guard posts only
when the head of the pull request has the tree that the agent reviewed, so each line is right. The
findings become open findings of the pull request. The post sets no status check and no approval: the
author of the change chose the reviewer, so the post is never Guard's verdict."""

from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe import _

from guard.git.repository import store_for
from guard.git.store import GitError, GitStore
from guard.permissions import REVIEWER_ROLES
from guard.providers import adapter_for_repository
from guard.review import links, local, pull_requests, render
from guard.review.findings import Finding, FindingFilter
from guard.review.publish import Publisher
from guard.review.run import ReviewRun, Verdict
from guard.utils import cache, db

if TYPE_CHECKING:
	from guard.guard.doctype.code_review.code_review import CodeReview
	from guard.guard.doctype.pull_request.pull_request import PullRequest

PUBLISH_LOCK_TTL = 600  # seconds: fetching the head and posting many comments can be slow
FINDING_FIELDS = (
	"name",
	"path",
	"start_line",
	"end_line",
	"severity",
	"comment_type",
	"category",
	"title",
	"body",
	"suggestion",
	"question",
	"confidence",
	"side",
	"status",
	"custom_context",
)


def publish(review: str) -> dict[str, Any]:
	"""Post the local review `review` of the signed-in user on the open pull request of its branch."""
	with cache.exclusive(f"guard:local-publish:{review}", PUBLISH_LOCK_TTL) as acquired:
		if not acquired:
			frappe.throw(_("Guard is posting review {0} now.").format(review))
		return LocalPost(review).post()


class LocalPost:
	def __init__(self, review: str):
		frappe.only_for(list(REVIEWER_ROLES), message=True)
		self.review = cast("CodeReview", frappe.get_doc("Code Review", review))
		if self.review.source != "local" or self.review.requested_by != frappe.session.user:
			frappe.throw(_("You can post only your own local reviews."), frappe.PermissionError)
		# The body is set by a post only, also by one with no inline comments.
		if self.review.posted_count or self.review.body:
			frappe.throw(_("Review {0} is on the pull request already.").format(review))
		self.repository = str(self.review.repository)

	def post(self) -> dict[str, Any]:
		pr = self._pull_request()
		adapter = adapter_for_repository(self.repository)
		if not adapter:
			frappe.throw(_("The repository has no code provider."))
		store = store_for(self.repository)
		head = self._reviewed_head(store, adapter, pr)
		run = self._run(store, pr, head)
		run.adapter = adapter
		names = {id(f): str(f.extra["name"]) for f in run.findings}
		LocalPublisher(run).publish(names, 0)
		posted = frappe.db.count("Review Finding", {"code_review": self.review.name, "posted": 1})
		self._link(pr, head, posted)
		# Commit before the lock ends, so a second call sees the post.
		db.commit()
		return {
			"review": self.review.name,
			"pull_request": pr.web_url or f"#{pr.number}",
			"posted": posted,
			"url": links.code_review(str(self.review.name)),
		}

	def _pull_request(self) -> "PullRequest":
		name = local.open_pull_request(self.repository, str(self.review.branch or ""))
		if not name:
			frappe.throw(
				_("Guard has no open pull request for branch {0}. Push it and open the pull request.").format(
					self.review.branch or "?"
				)
			)
			raise AssertionError  # frappe.throw raises
		return cast("PullRequest", frappe.get_doc("Pull Request", name))

	def _reviewed_head(self, store: GitStore, adapter: Any, pr: "PullRequest") -> str:
		"""The head of the pull request, when its tree is the tree that the agent reviewed."""
		try:
			head = pull_requests.fetch_head(store, adapter, pr)
			tree = store.tree_of(head)
		except GitError:
			frappe.throw(_("Guard could not fetch the head of the pull request. Try again."))
			raise
		if not self.review.tree_sha or tree != self.review.tree_sha:
			frappe.throw(
				_(
					"The head of the pull request is not the code that you reviewed. Push your change, or "
					"review the pull request head again."
				)
			)
		return head

	def _run(self, store: GitStore, pr: "PullRequest", head: str) -> ReviewRun:
		"""The run of the pull request at `head`, with the findings and the verdict of the review."""
		base = store.merge_base(store.rev_parse(f"refs/heads/{pr.base_branch}"), head)
		run = local.change_run(
			self.repository, store, base, head, store.diff(base, head), str(self.review.branch or "")
		)
		run.review, run.pr = self.review, pr
		run.commits = store.commits_between(base, head)
		run.verdict = Verdict.from_arguments(self.review.as_dict())
		run.findings = self._findings()
		# Anchor the open findings again, on the diff of the pull request.
		anchors = FindingFilter(run.files, run.settings)
		for finding in run.findings:
			if finding.kept:
				anchors.anchor(finding)
		return run

	def _findings(self) -> list[Finding]:
		rows = frappe.get_all(
			"Review Finding", filters={"code_review": self.review.name}, fields=list(FINDING_FIELDS)
		)
		return [_finding(row) for row in rows]

	def _link(self, pr: "PullRequest", head: str, posted: int) -> None:
		"""Make the findings open findings of the pull request, as those of a review on GitHub."""
		frappe.db.set_value(
			"Review Finding",
			{"code_review": self.review.name},
			"pull_request",
			pr.name,
			update_modified=False,
		)
		self.review.db_set({"pull_request": pr.name, "head_sha": head, "posted_count": posted})


class LocalPublisher(Publisher):
	"""Post the findings, then a new summary under them. Guard's own summary comment, status check
	and reactions stay as they are."""

	def publish(self, names: dict[int, str], review_number: int) -> None:
		posted = [f for f in self.run.findings if f.kept]
		urls = self._inline([f for f in posted if not f.outside_diff], names)
		body = render.summary(self.run, posted, review_number, urls)
		self.run.review.db_set("body", body)
		self.gh.post_comment(self.repo, self.number, body)


def _finding(row: dict[str, Any]) -> Finding:
	line = int(row.get("start_line") or 1)
	return Finding(
		str(row["path"]),
		line,
		int(row.get("end_line") or line),
		str(row["severity"]),
		str(row.get("comment_type") or "logic"),
		str(row["title"]),
		str(row.get("body") or ""),
		category=str(row.get("category") or "correctness"),
		suggestion=str(row.get("suggestion") or ""),
		question=str(row.get("question") or ""),
		context_name=row.get("custom_context"),
		confidence=float(row.get("confidence") or 0.7),
		side=str(row.get("side") or "RIGHT"),
		status=str(row["status"]),
		extra={"name": row["name"]},
	)
