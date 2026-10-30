"""Write findings, the summary, the status and reactions to GitHub and to the database."""

import frappe

from guard.learning import memories
from guard.providers.base import InlineComment, PostedComment, ProviderAdapter, ProviderError
from guard.review import links, render
from guard.review.findings import Finding
from guard.review.rule_files import RULE_PREFIX
from guard.review.run import ReviewRun
from guard.utils import db

# The check of a review in an outage: no model read the commit yet (`ReviewRun.outage`).
OUTAGE_TEXT = "Checks posted. The AI review waits for an AI provider that is down"


class Publisher:
	def __init__(self, run: ReviewRun):
		self.run = run
		self.adapter = run.adapter
		self.repo = run.full_name
		self.number = int(run.pr.number) if run.pr is not None else 0

	@property
	def gh(self) -> ProviderAdapter:
		if self.adapter is None:
			raise ProviderError("The repository has no code provider")
		return self.adapter

	# ------------------------------------------------------------ records

	def save_findings(self) -> dict[int, str]:
		"""One Review Finding for every finding, kept or not. Returns id(finding) -> record name."""
		names: dict[int, str] = {}
		rules = self._rule_contexts()
		for finding in self.run.findings:
			if finding.rule_id and not finding.context_name:
				finding.context_name = rules.get(finding.rule_id) or memories.cited(finding.rule_id)
			doc = frappe.get_doc(
				{
					"doctype": "Review Finding",
					"code_review": self.run.review.name,
					"pull_request": self.run.pr.name if self.run.pr is not None else None,
					"repository": self.run.repository,
					"title": finding.title,
					"severity": finding.severity,
					"comment_type": finding.comment_type,
					"category": finding.category,
					"is_security": int(finding.is_security),
					"status": finding.status,
					"status_reason": finding.status_reason,
					"confidence": finding.confidence,
					"outside_diff": int(finding.outside_diff),
					"verified": int(finding.verified),
					"path": finding.path,
					"start_line": finding.start_line,
					"end_line": finding.end_line,
					"side": finding.side,
					"fingerprint": finding.fingerprint,
					"body": finding.body,
					"suggestion": finding.suggestion,
					"question": finding.question,
					"fix_prompt": render.fix_prompt(finding),
					"custom_context": finding.context_name,
					"author": self.run.author,
					"sandbox_log": finding.sandbox_log,
				}
			).insert(ignore_permissions=True)
			names[id(finding)] = str(doc.name)
		return names

	# ------------------------------------------------------------ github

	def _rule_contexts(self) -> dict[str, str]:
		"""rule_id -> Custom Context name, for dashboard rules in this review."""
		mapping: dict[str, str] = {}
		if self.run.config:
			for file_config in self.run.config.files.values():
				for rule in file_config.rules:
					if rule.id and rule.context_name:
						mapping[rule.id] = rule.context_name
		return mapping

	def publish(self, names: dict[int, str], review_number: int) -> None:
		if not self.adapter or self.run.pr is None:
			return
		posted = [f for f in self.run.findings if f.kept]
		inline = (
			[] if self.run.settings.get("update_summary_only") else [f for f in posted if not f.outside_diff]
		)
		if inline and not self.run.settings.get("update_description") and not self.run.pr.summary_comment_id:
			# GitHub orders the timeline by creation time. Post the summary before the inline comments so
			# that it shows above them, then edit it below to add the links to them.
			self._new_summary(render.summary(self.run, posted, review_number, {}))
			# Do not keep the Pull Request row locked through the GitHub calls for the inline comments.
			db.commit()
		urls = self._inline(inline, names)
		body = render.summary(self.run, posted, review_number, urls)
		self.run.review.db_set("body", body)
		# Do not keep the Code Review row locked through the GitHub calls and the Pull Request write below.
		db.commit()
		if self.run.settings.get("update_description"):
			text = render.description(self.run.description, body.replace(render.SUMMARY_MARKER, ""))
			self.gh.update_description(self.repo, self.number, text)
		elif self.run.pr.summary_comment_id:
			try:
				self.gh.update_comment(self.repo, self.number, self.run.pr.summary_comment_id, body)
			except ProviderError:
				self._new_summary(body)
		else:
			self._new_summary(body)

	def _new_summary(self, body: str) -> None:
		comment = self.gh.post_comment(self.repo, self.number, body)
		self.run.pr.db_set("summary_comment_id", comment.id)

	def _rule_link(self, rule_id: str) -> str:
		"""`[CLAUDE.md line 50](url)` for `project:CLAUDE.md:50`, at the commit the rule file came from."""
		path, _, line = rule_id.removeprefix(RULE_PREFIX).rpartition(":")
		label = f"{path} line {line}"
		if not line.isdigit():
			return f"`{rule_id.removeprefix(RULE_PREFIX)}`"
		try:
			url = self.gh.file_url(self.repo, self.run.config_sha or self.run.head_sha, path, int(line))
		except NotImplementedError:
			return f"`{label}`"
		return f"[{label}]({url})"

	def _inline(self, findings: list[Finding], names: dict[int, str]) -> dict[int, str]:
		if not findings:
			return {}
		fix = bool(self.run.settings.get("fix_with_ai", True))
		for finding in findings:
			if finding.is_project_rule:
				finding.extra["rule_link"] = self._rule_link(str(finding.rule_id))
		comments = [_comment(f, names[id(f)], fix) for f in findings]
		urls: dict[int, str] = {}
		try:
			posted = self.gh.create_review(self.repo, self.number, self.run.head_sha, comments)
		except ProviderError:
			posted: list[PostedComment] = []
			for comment in comments:
				try:
					posted += self.gh.create_review(self.repo, self.number, self.run.head_sha, [comment])
				except ProviderError:
					posted.append(PostedComment(""))
		for finding, result in zip(findings, posted, strict=False):
			name = names[id(finding)]
			if result and result.id:
				db.set_values(
					"Review Finding",
					name,
					{
						"posted": 1,
						"provider_comment_id": result.id,
						"provider_thread_id": result.thread_id,
						"comment_url": result.url,
					},
				)
				urls[id(finding)] = result.url
			else:
				finding.outside_diff = True
				frappe.db.set_value("Review Finding", name, "outside_diff", 1)
		return urls

	# ------------------------------------------------------------ status

	def start(self) -> None:
		if not self.adapter or self.run.pr is None:
			return
		if eyes := self._react("eyes"):
			self.run.review.db_set("eyes_reaction", eyes, update_modified=False)
		if self.run.settings.get("status_check", True):
			self._status("pending", "Guard is reviewing")
		elif self._check_id():
			# Queued before the config of the repository was read, which turns the check off.
			self._status("skipped", "This repository turned the Guard check off.")
		# A cancel from now on reads the ids to end the check and remove the 👀.
		db.commit()

	def finish(self, failed: bool = False) -> None:
		if not self.adapter or self.run.pr is None:
			return
		remove_eyes(self.gh, self.repo, self.number, str(self.run.review.name))
		self._react("confused" if failed else "+1")
		if failed:
			self._status("error", "Guard could not finish the review")
			return
		posted = [f for f in self.run.findings if f.kept]
		if self.run.settings.get("status_check", True):
			score = self.run.verdict.confidence_score
			open_count = self._open_findings()
			text = f"{score}/5 · {open_count} open finding(s)"
			if self.run.outage:
				# The full review follows, so this is not the result. A required check stays open, as
				# it does while reviews wait for the AI provider.
				self._status("queued", f"{OUTAGE_TEXT} · {open_count} open finding(s)")
			else:
				self._status("success" if score == 5 and not open_count else "failure", text)
		elif not posted and self.run.settings.get("status_comments"):
			self.adapter.post_comment(
				self.repo, self.number, f"{len(self.run.files)} files reviewed, no comments."
			)

	def _open_findings(self) -> int:
		"""Open findings on the pull request, from this review and earlier ones."""
		if not self.run.review.pull_request:
			return sum(1 for f in self.run.findings if f.kept)
		return open_finding_count(str(self.run.review.pull_request))

	def skip(self, reason: str) -> None:
		if self._shows_check():
			self._keep_check(
				report_skip(self.gh, self.repo, self.run.head_sha, reason, self._url(), self._check_id())
			)

	def _shows_check(self) -> bool:
		return bool(self.adapter and self.run.pr is not None and self.run.settings.get("status_check", True))

	def _status(self, state: str, text: str) -> None:
		self._keep_check(
			set_status(self.gh, self.repo, self.run.head_sha, state, text, self._url(), self._check_id())
		)

	def _check_id(self) -> str:
		return str(self.run.review.check_run_id or "")

	def _keep_check(self, check_id: str) -> None:
		"""Keep the id of the check, so that a cancel or a dismissed finding can update it at once."""
		if check_id and check_id != self._check_id():
			self.run.review.db_set("check_run_id", check_id, update_modified=False)

	def _url(self) -> str:
		return links.code_review(str(self.run.review.name))

	def _react(self, content: str) -> str:
		try:
			return self.gh.add_reaction(self.repo, self.number, "pr", "", content) or ""
		except ProviderError:
			return ""


def remove_eyes(adapter: ProviderAdapter, repo: str, number: int, review: str) -> None:
	"""Remove the 👀 that the review put on the pull request when it started."""
	reaction = db.value("Code Review", review, "eyes_reaction")
	if not reaction:
		return
	try:
		adapter.remove_reaction(repo, number, "pr", "", str(reaction))
	except ProviderError:
		pass
	frappe.db.set_value("Code Review", review, "eyes_reaction", "", update_modified=False)


def set_status(
	adapter: ProviderAdapter, repo: str, sha: str, state: str, text: str, url: str = "", check_id: str = ""
) -> str:
	"""Set the Guard check and return its id. A provider error is only logged."""
	try:
		return adapter.set_status(repo, sha, state, text, url, check_id) or check_id
	except ProviderError as error:
		frappe.log_error(str(error), "Guard status")
		return check_id


def report_skip(
	adapter: ProviderAdapter, repo: str, sha: str, reason: str, url: str = "", check_id: str = ""
) -> str:
	"""Show on the commit that Guard will not review it, so nobody waits for a review."""
	return set_status(adapter, repo, sha, "skipped", f"Guard skipped this review: {reason}", url, check_id)


def open_finding_count(pull_request: str) -> int:
	"""Open findings on the pull request from every review, inline or only in the summary."""
	return int(
		db.scalar(
			"SELECT COUNT(*) FROM `tabReview Finding` WHERE pull_request = %s AND status = 'Open'",
			(pull_request,),
		)
		or 0
	)


def _comment(finding: Finding, name: str, fix: bool) -> InlineComment:
	start = finding.start_line if finding.start_line < finding.end_line else None
	return InlineComment(
		finding.path, finding.end_line, render.inline(finding, name, fix), start, finding.side
	)
