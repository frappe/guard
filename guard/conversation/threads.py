"""Mentions and replies on pull requests: classify, answer, learn."""

from typing import TYPE_CHECKING, cast

import frappe

from guard.config.resolver import ConfigResolver
from guard.conversation import acks, mentions
from guard.conversation import answer as answerer
from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.graph.queries import CodeGraph
from guard.learning import feedback, missed
from guard.providers import adapter_for_repository
from guard.providers.base import ProviderComment, ProviderError
from guard.review import permissions, tips, trigger
from guard.review.run import ReviewRun
from guard.utils import db

if TYPE_CHECKING:
	from guard.guard.doctype.pull_request.pull_request import PullRequest


def run_for(pull_request: str) -> ReviewRun:
	"""A read-only view of the pull request at its head, for conversations."""
	pr = cast("PullRequest", frappe.get_doc("Pull Request", pull_request))
	repository = str(pr.repository)
	store = store_for(repository)
	adapter = adapter_for_repository(repository)
	number = int(pr.number)
	head = pr.head_sha or ""
	if not head or not store.has_commit(head):
		store.fetch_branches(pr.base_branch or "")
		head = store.fetch_pull(adapter.pull_ref(number), number) if adapter else head
	merge_base = store.merge_base(store.rev_parse(f"refs/heads/{pr.base_branch}"), head)
	files = parse_diff(store.diff(merge_base, head))
	index = db.value("Code Repository", repository, "default_index")
	return ReviewRun(
		review=None,
		repository=repository,
		full_name=str(db.value("Code Repository", repository, "full_name")),
		head=lambda path: store.show(head, path),
		base=lambda path: store.show(merge_base, path),
		list_dir=lambda path: store.list_dir(head, path),
		head_sha=head,
		base_sha=merge_base,
		grep=lambda pattern, glob: store.grep(head, pattern, glob),
		title=pr.title or "",
		description=pr.description or "",
		author=pr.author or "",
		base_branch=pr.base_branch or "",
		head_branch=pr.head_branch or "",
		pr=pr,
		adapter=adapter,
		all_files=files,
		files=files,
		config=ConfigResolver.from_store(repository, store, head).resolve_pr([f.path for f in files]),
		graph=CodeGraph(str(index)) if index else None,
	)


def handle(pull_request: str, comment: ProviderComment) -> None:
	"""Entry point for every new human comment on a pull request."""
	repository = str(db.value("Pull Request", pull_request, "repository"))
	text = mentions.find(comment.body)
	finding = _finding_of(pull_request, comment)
	if finding:
		_finding_reply(pull_request, repository, comment, finding, text)
	elif text is not None:
		_mention(pull_request, repository, comment, text)
	else:
		missed.consider(pull_request, repository, comment)


def _finding_reply(
	pull_request: str, repository: str, comment: ProviderComment, finding: str, text: str | None
) -> None:
	reading = feedback.read_reply(comment.body, _thread_text(pull_request, comment, finding))
	if permissions.allowed("teach", comment.author, repository):
		feedback.record_reply(finding, pull_request, comment, reading.sentiment)
	if text is not None:
		_mention(pull_request, repository, comment, text)
	else:
		_thread_reply(pull_request, repository, comment, finding, reading.kind)


def _mention(pull_request: str, repository: str, comment: ProviderComment, text: str) -> None:
	mention = mentions.parse(text)
	if mention.intent == "review":
		# 👀 first: the review can finish inside consider() when it runs inline.
		ack = acks.accept(pull_request, comment)
		acks.wait_for_review(ack)
		if not trigger.consider(
			pull_request, "open", True, comment.author, mention.tier, mention.text, force_draft=mention.draft
		):
			acks.withdraw(ack)
			acks.forget(ack)
		return
	if mention.intent == "ignore":
		return
	if mention.intent == "feedback":
		if permissions.allowed("teach", comment.author, repository):
			feedback.record_mention(pull_request, comment, mention.text)
		_react(pull_request, comment, "+1")
		if "?" not in mention.text:
			return
	if not permissions.allowed("chat", comment.author, repository):
		return
	_answer(pull_request, comment, mention.text)


def _thread_reply(
	pull_request: str, repository: str, comment: ProviderComment, finding: str, kind: str
) -> None:
	if kind == "ack":
		_react(pull_request, comment, "+1")
	elif kind == "answer" and permissions.allowed("chat", comment.author, repository):
		question = db.value("Review Finding", finding, "question")
		if question:
			feedback.check_clarification(finding, comment.body)
		_answer(pull_request, comment, comment.body, finding)


def _answer(pull_request: str, comment: ProviderComment, question: str, finding: str | None = None) -> None:
	"""Reply with 👀 while Guard works on it, and 👍 when the reply is posted."""
	ack = acks.accept(pull_request, comment)
	try:
		posted = _reply(pull_request, comment, question, finding)
	except Exception:
		acks.done(ack, failed=True)
		raise
	acks.done(ack, failed=not posted)


def _reply(pull_request: str, comment: ProviderComment, question: str, finding: str | None = None) -> bool:
	run = run_for(pull_request)
	thread = _thread_text(pull_request, comment, finding)
	result = answerer.answer(run, question, thread)
	tip = tips.after_reply(str(run.repository), comment.author) if finding else ""
	if tip:
		result.text = f"{result.text}\n\n{tip}"
	if not (run.adapter and result.text):
		return False
	try:
		run.adapter.reply_to_comment(run.full_name, int(run.pr.number), comment, result.text)
	except ProviderError as error:
		frappe.log_error(str(error), "Guard reply")
		return False
	return True


def _thread_text(pull_request: str, comment: ProviderComment, finding: str | None) -> str:
	parts = []
	if finding:
		row = db.values("Review Finding", finding, ("title", "body", "path", "start_line")) or {}
		parts.append(
			f"Guard finding at {row.get('path')}:{row.get('start_line')}: {row.get('title')}\n{row.get('body')}"
		)
	if comment.thread_id:
		for author, body in db.rows(
			"SELECT author, body FROM `tabPull Request Comment` WHERE pull_request = %s AND thread_id = %s ORDER BY commented_at",
			(pull_request, comment.thread_id),
		):
			parts.append(f"{author}: {body}")
	if comment.path:
		parts.append(f"The comment is on {comment.path}:{comment.line}")
	return "\n\n".join(parts)


def _finding_of(pull_request: str, comment: ProviderComment) -> str | None:
	if not comment.thread_id and not comment.in_reply_to:
		return None
	for key in (k for k in (comment.in_reply_to, comment.thread_id) if k):
		name = db.value("Review Finding", {"pull_request": pull_request, "provider_comment_id": key}, "name")
		if name:
			return str(name)
	return None


def _react(pull_request: str, comment: ProviderComment, content: str) -> None:
	repository = str(db.value("Pull Request", pull_request, "repository"))
	adapter = adapter_for_repository(repository)
	if not adapter:
		return
	target = "inline" if comment.kind == "inline" else "issue"
	try:
		adapter.add_reaction(
			str(db.value("Code Repository", repository, "full_name")),
			int(db.value("Pull Request", pull_request, "number") or 0),
			target,
			comment.id,
			content,
		)
	except ProviderError:
		pass
