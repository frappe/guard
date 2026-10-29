"""Process one stored Webhook Delivery."""

import json
from typing import TYPE_CHECKING, cast

import frappe
from frappe.utils import add_to_date, now_datetime
from frappe.utils.background_jobs import is_job_enqueued

from guard import settings as guard_settings
from guard.learning import addressed, feedback
from guard.llm import breaker
from guard.llm.types import GatewayBusy
from guard.providers import sync
from guard.providers.base import ProviderComment
from guard.review import pull_requests, trigger
from guard.utils import db
from guard.utils.redact import redact
from guard.utils.timeutil import parse_iso
from guard.webhooks import github
from guard.webhooks.events import WebhookEvent

if TYPE_CHECKING:
	from guard.guard.doctype.pull_request_comment.pull_request_comment import PullRequestComment

OPEN_ACTIONS = {"opened", "reopened", "ready"}
# An answer makes up to 9 gateway calls, and each one can wait for a rate limit.
COMMENT_TIMEOUT = 1800
# Seconds before each new attempt of a comment that met a busy or failing gateway.
COMMENT_RETRY_DELAYS = (120, 600, 1800)
# A delivery with no job after 5 minutes lost it: a short job waits seconds in a healthy queue.
LOST_DELIVERY_AFTER = 300
# Events older than 6 hours are not handled late. Guard polls open pull requests when polling is on.
LOST_DELIVERY_MAX_AGE = 6 * 3600
MAX_DELIVERY_ATTEMPTS = 3


def enqueue_delivery(delivery: str) -> None:
	frappe.enqueue(
		"guard.webhooks.handler.process",
		queue="short",
		job_id=delivery_job_id(delivery),
		deduplicate=True,
		enqueue_after_commit=True,
		delivery=delivery,
	)


def delivery_job_id(delivery: str) -> str:
	return f"guard-delivery-{delivery}"


def requeue_lost_deliveries() -> None:
	"""Every 5 minutes: queue again a delivery that is still Received and has no job.

	The Webhook Delivery is the truth, not the queue. A deploy or a dead Redis can lose the job, and
	then nobody handles the event. Old events are not handled: a review or a reply hours late does
	more harm than good. A delivery whose job stops every time, for example at the job timeout,
	fails after MAX_DELIVERY_ATTEMPTS.
	"""
	now = now_datetime()
	rows = db.dict_rows(
		"""SELECT name, attempt FROM `tabWebhook Delivery`
		WHERE status = 'Received' AND creation BETWEEN %s AND %s ORDER BY creation LIMIT %s""",
		(
			add_to_date(now, seconds=-LOST_DELIVERY_MAX_AGE),
			add_to_date(now, seconds=-LOST_DELIVERY_AFTER),
			500,
		),
	)
	for row in rows:
		name = str(row["name"])
		if is_job_enqueued(delivery_job_id(name)):
			continue
		attempt = int(row["attempt"] or 0) + 1
		if attempt > MAX_DELIVERY_ATTEMPTS:
			db.set_values(
				"Webhook Delivery",
				name,
				{"status": "Failed", "error": "The job of this delivery stopped each time before it ended."},
			)
			continue
		db.set_values("Webhook Delivery", name, {"attempt": attempt})
		enqueue_delivery(name)
	db.commit()


def process(delivery: str) -> None:
	doc = frappe.get_doc("Webhook Delivery", delivery)
	try:
		payload = json.loads(str(doc.get("payload") or "{}"))
		events = github.parse(str(doc.get("event")), payload)
		handled = False
		for event in events:
			handled = dispatch(str(doc.get("code_provider")), event) or handled
		doc.db_set("status", "Processed" if handled else "Ignored")
	except frappe.QueryDeadlockError, frappe.QueryTimeoutError:
		# The database rolled back the work. Frappe runs the job again.
		raise
	except Exception as error:
		db.rollback()
		doc.db_set({"status": "Failed", "error": redact(f"{type(error).__name__}: {error}")[:5000]})
		db.commit()
		if frappe.flags.in_test:
			raise


def dispatch(provider: str, event: WebhookEvent) -> bool:
	if event.kind == "installation":
		sync.on_installation(
			provider, event.action, event.namespace, event.installation_id, event.repositories
		)
		return True
	repository = sync.resolve_repository(provider, event.repository, event.namespace, event.installation_id)
	if not repository:
		return False
	if event.kind == "push":
		return _push(repository, event)
	if event.kind == "pull_request" and event.pull_request:
		return _pull_request(repository, event)
	if event.kind == "comment" and event.comment and event.number:
		return _comment(repository, event)
	if event.kind == "thread" and event.thread_id and event.number:
		return _thread(repository, event)
	if event.kind == "rerun" and event.number:
		pr = pull_requests.refresh(repository, event.number)
		# Release the Pull Request row before the git work of the gate.
		db.commit()
		return bool(trigger.consider(str(pr.name), "retrigger", True, event.sender))
	return False


def _push(repository: str, event: WebhookEvent) -> bool:
	default = db.value("Code Repository", repository, "default_branch")
	if event.ref != default:
		return False
	from guard.indexing.jobs import enqueue_index

	enqueue_index(repository)
	if db.value("Code Repository", repository, "kb_status") == "Ready":
		frappe.enqueue(
			"guard.knowledge.builder.on_push",
			queue="long",
			enqueue_after_commit=True,
			repository=repository,
			before=event.before,
			after=event.after,
		)
	return True


def _pull_request(repository: str, event: WebhookEvent) -> bool:
	info = event.pull_request
	assert info is not None
	before_head = db.value("Pull Request", {"repository": repository, "number": info.number}, "head_sha")
	pr = pull_requests.upsert(repository, info)
	# Release the Pull Request row before the git work below. A review that ends writes it too.
	db.commit()
	name = str(pr.name)
	action = event.action
	if action in OPEN_ACTIONS:
		trigger.consider(name, "open")
		return True
	if action == "labeled" and event.label and trigger.trigger_label(name, event.label):
		trigger.consider(name, "open")
		return True
	if action == "synchronize":
		before = event.before or before_head
		from guard.learning import approve

		approve.dismiss(name)
		db.commit()
		if before and event.after:
			addressed.enqueue("on_push", pull_request=name, before=before, after=event.after)
		trigger.consider(name, trigger.push_kind(repository, before, event.after))
		return True
	if action in ("closed", "merged"):
		from guard.review import state

		for review in frappe.get_all(
			"Code Review",
			filters={"pull_request": name, "status": ["in", ["Queued", "Running"]]},
			pluck="name",
		):
			try:
				state.cancel(review, state.CLOSED)
			except state.NotActive:
				pass  # It posts its result now.
		addressed.enqueue("on_close", pull_request=name)
		return True
	return False


def _comment(repository: str, event: WebhookEvent) -> bool:
	comment = event.comment
	assert comment is not None and event.number is not None
	if event.action not in ("created", ""):
		return False
	pr = pull_requests.ensure(repository, event.number)
	is_guard = comment.author.lower() in guard_settings.bot_logins() or "<!-- guard:" in comment.body
	store_comment(str(pr.name), repository, comment, is_guard)
	if is_guard or comment.is_bot:
		return True
	enqueue_comment(str(pr.name), comment)
	return True


def _thread(repository: str, event: WebhookEvent) -> bool:
	assert event.thread_id is not None
	pull_request = db.value("Pull Request", {"repository": repository, "number": event.number}, "name")
	if not pull_request:
		return False
	return feedback.thread_changed(
		str(pull_request), event.thread_id, event.sender, resolved=event.action == "resolved"
	)


def enqueue_comment(pull_request: str, comment: ProviderComment, attempt: int = 0) -> None:
	frappe.enqueue(
		"guard.webhooks.handler.handle_comment",
		queue="default",
		timeout=COMMENT_TIMEOUT,
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		pull_request=pull_request,
		comment=comment.__dict__,
		attempt=attempt,
	)


def handle_comment(pull_request: str, comment: dict, attempt: int = 0) -> None:
	"""Read one human comment. When the gateway is busy or down, try it again later."""
	from guard.conversation import threads

	parsed = ProviderComment(**comment)
	try:
		threads.handle(pull_request, parsed)
	except GatewayBusy as busy:
		if attempt >= len(COMMENT_RETRY_DELAYS):
			threads.give_up(pull_request, parsed)
			raise
		delay = max(busy.retry_after, COMMENT_RETRY_DELAYS[attempt], breaker.wait_seconds([busy.ai_provider]))
		name = db.value(
			"Pull Request Comment", {"pull_request": pull_request, "provider_comment_id": parsed.id}, "name"
		)
		if not name:
			raise
		db.set_values(
			"Pull Request Comment",
			str(name),
			{"attempt": attempt + 1, "retry_at": add_to_date(now_datetime(), seconds=delay)},
		)


def resume_comments() -> None:
	"""Every minute: queue again the comments whose retry time has passed."""
	for row in frappe.get_all(
		"Pull Request Comment",
		filters=db.due("retry_at"),
		fields=["name", "pull_request", "attempt"],
	):
		db.set_values("Pull Request Comment", row.name, {"retry_at": None})
		enqueue_comment(row.pull_request, stored_comment(row.name), int(row.attempt or 0))


def stored_comment(name: str) -> ProviderComment:
	row = cast("PullRequestComment", frappe.get_doc("Pull Request Comment", name))
	return ProviderComment(
		id=row.provider_comment_id or "",
		kind=row.kind or "issue",
		author=row.author or "",
		body=row.body or "",
		created_at=str(row.commented_at or ""),
		path=row.path or None,
		line=row.line or None,
		in_reply_to=row.in_reply_to or None,
		thread_id=row.thread_id or None,
		is_bot=bool(row.is_bot),
		number=int(db.value("Pull Request", row.pull_request, "number") or 0) or None,
	)


def store_comment(pull_request: str, repository: str, comment: ProviderComment, is_guard: bool) -> None:
	if frappe.db.exists(
		"Pull Request Comment", {"pull_request": pull_request, "provider_comment_id": comment.id}
	):
		return
	finding = (
		db.value(
			"Review Finding",
			{
				"pull_request": pull_request,
				"provider_comment_id": comment.in_reply_to or comment.thread_id or "",
			},
			"name",
		)
		if (comment.in_reply_to or comment.thread_id)
		else None
	)
	frappe.get_doc(
		{
			"doctype": "Pull Request Comment",
			"pull_request": pull_request,
			"repository": repository,
			"provider_comment_id": comment.id,
			"kind": comment.kind,
			"thread_id": comment.thread_id,
			"in_reply_to": comment.in_reply_to,
			"author": comment.author,
			"is_bot": int(comment.is_bot),
			"is_guard": int(is_guard),
			"body": comment.body,
			"path": comment.path,
			"line": comment.line,
			"commented_at": parse_iso(comment.created_at),
			"finding": finding,
		}
	).insert(ignore_permissions=True)
