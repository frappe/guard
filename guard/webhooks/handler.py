"""Process one stored Webhook Delivery."""

import json

import frappe

from guard import settings as guard_settings
from guard.providers import sync
from guard.providers.base import ProviderComment
from guard.review import pull_requests, trigger
from guard.utils import db
from guard.utils.redact import redact
from guard.utils.timeutil import parse_iso
from guard.webhooks import github
from guard.webhooks.events import WebhookEvent

OPEN_ACTIONS = {"opened", "reopened", "ready"}


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
	if event.kind == "rerun" and event.number:
		pr = pull_requests.refresh(repository, event.number)
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
			frappe.enqueue(
				"guard.learning.addressed.on_push",
				queue="default",
				enqueue_after_commit=True,
				now=bool(frappe.flags.in_test),
				pull_request=name,
				before=before,
				after=event.after,
			)
		trigger.consider(name, trigger.push_kind(repository, before, event.after))
		return True
	if action in ("closed", "merged"):
		for review in frappe.get_all(
			"Code Review",
			filters={"pull_request": name, "status": ["in", ["Queued", "Running"]]},
			pluck="name",
		):
			frappe.db.set_value("Code Review", review, "cancel_requested", 1)
		frappe.enqueue(
			"guard.learning.addressed.on_close",
			queue="default",
			enqueue_after_commit=True,
			now=bool(frappe.flags.in_test),
			pull_request=name,
		)
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
	frappe.enqueue(
		"guard.webhooks.handler.handle_comment",
		queue="default",
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		pull_request=str(pr.name),
		comment=comment.__dict__,
	)
	return True


def handle_comment(pull_request: str, comment: dict) -> None:
	from guard.conversation import threads

	threads.handle(pull_request, ProviderComment(**comment))


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
