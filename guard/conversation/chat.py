"""Chat with PR and chat actions. See docs/spec/07-conversations.md."""

import json
from typing import Any

import frappe
from frappe import _

from guard.conversation import answer as answerer
from guard.conversation.threads import run_for
from guard.llm.client import LLMClient
from guard.llm.types import Message, ToolSpec
from guard.providers.base import ProviderComment, ProviderError, UnsupportedAction
from guard.review.prompts import data
from guard.utils import db

ROUTINE = {"post_comment", "reply", "resolve_thread", "reopen_thread", "request_review"}
ACTIONS = ROUTINE | {
	"approve",
	"request_changes",
	"update_description",
	"update_branch",
	"merge",
	"close",
	"reopen",
	"create_rule",
}
MODES = ("Ask every time", "Allow routine actions", "Full access")
CHAT_SYSTEM = (
	answerer.SYSTEM
	+ """
You can propose actions on the pull request with `propose_action`. Propose an action only when
the user asks for it, or when it clearly helps. Set `draft` to true when the user asks for a draft
or a preview: drafts are never run."""
)

PROPOSE = ToolSpec(
	"propose_action",
	"Propose an action on the pull request. The user approves it before it runs.",
	{
		"type": "object",
		"properties": {
			"action": {"type": "string", "enum": sorted(ACTIONS)},
			"summary": {"type": "string", "description": "One line the user sees"},
			"body": {
				"type": "string",
				"description": "Comment, reply, review or description text, or the rule",
			},
			"comment_id": {"type": "string", "description": "For reply, resolve_thread, reopen_thread"},
			"reviewers": {"type": "array", "items": {"type": "string"}},
			"method": {"type": "string", "enum": ["merge", "squash", "rebase"]},
			"draft": {"type": "boolean"},
		},
		"required": ["action", "summary"],
	},
)


@frappe.whitelist(methods=["POST"])
def send(
	pull_request: str, message: str, session: str | None = None, context: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
	"""Ask a question about a pull request. Returns the session, the answer and any actions."""
	frappe.has_permission("Pull Request", "read", pull_request, throw=True)
	doc = _session(pull_request, session)
	run = run_for(pull_request)
	history = [Message(m.role, m.content) for m in doc.get("messages")[-20:]]
	selected = "\n\n".join(
		data(str(c.get("label") or "selection"), str(c.get("text") or "")) for c in context or []
	)
	prompt = "\n\n".join(
		p
		for p in [
			"Pull request:\n" + data("pull request", f"{run.title}\n\n{run.description}"),
			"Diff:\n" + data("diff", "\n\n".join(f.annotated() for f in run.files)[:50_000]),
			("Selected context:\n" + selected) if selected else "",
			"Question:\n" + data("question", message),
		]
		if p
	)
	client = LLMClient(purpose="conversation", usage=run.usage)
	messages = [Message("system", CHAT_SYSTEM, cache=True), *history, Message("user", prompt)]
	tools = [*answerer.read_tools(run), PROPOSE]
	proposed: list[dict[str, Any]] = []
	text = ""
	for _turn in range(answerer.MAX_TURNS):
		result = client.chat(messages, tools=tools)
		messages.append(result.as_message())
		if not result.tool_calls:
			text = result.content.strip()
			break
		for call in result.tool_calls:
			if call.name == "propose_action":
				proposed.append(call.arguments)
				reply = "proposed"
			else:
				reply = answerer._tool(run, call.name, call.arguments)
			messages.append(Message("tool", reply[:8000], tool_call_id=call.id))
	doc.append("messages", {"role": "user", "content": message, "context": frappe.as_json(context or [])})
	actions = [_propose(doc, args) for args in proposed if args.get("action") in ACTIONS]
	doc.append(
		"messages",
		{
			"role": "assistant",
			"content": text or (_("I proposed an action.") if actions else ""),
			"actions": frappe.as_json([a["name"] for a in actions]),
		},
	)
	doc.save()
	for action in actions:
		if allowed_without_asking(doc, action) and not action["draft"]:
			action.update(execute(action["name"]))
	return {"session": doc.name, "answer": text, "actions": actions}


@frappe.whitelist(methods=["POST"])
def direct(pull_request: str, action: str, body: str = "", method: str = "merge") -> dict[str, Any]:
	"""A button click in Desk: the click is the approval. Recorded as an executed Chat Action."""
	frappe.has_permission("Pull Request", "read", pull_request, throw=True)
	if action not in ACTIONS:
		frappe.throw(_("Unknown action {0}").format(action))
	session = _session(pull_request, None)
	proposal = _propose(
		session, {"action": action, "summary": f"{action} from Desk", "body": body, "method": method}
	)
	frappe.db.set_value("Chat Action", proposal["name"], "status", "Approved")
	return execute(proposal["name"])


@frappe.whitelist(methods=["POST"])
def decide(
	action: str, approve: bool, body: str | None = None, allow_for_chat: bool = False
) -> dict[str, Any]:
	doc = frappe.get_doc("Chat Action", action)
	doc.check_permission("write")
	if doc.get("status") != "Proposed":
		frappe.throw(_("This action is already {0}.").format(doc.get("status")))
	if not approve:
		doc.db_set("status", "Cancelled")
		return {"status": "Cancelled"}
	if body is not None:
		payload = json.loads(str(doc.get("payload") or "{}"))
		payload["body"] = body
		doc.db_set("payload", frappe.as_json(payload))
	if allow_for_chat and doc.get("routine"):
		set_mode(str(doc.get("chat_session")), "Allow routine actions")
	doc.db_set("status", "Approved")
	return execute(action)


@frappe.whitelist(methods=["POST"])
def set_mode(session: str, mode: str) -> dict[str, Any]:
	"""Change the permission mode. Waiting actions that the new mode allows run now."""
	if mode not in MODES:
		frappe.throw(_("Unknown mode {0}").format(mode))
	doc = frappe.get_doc("Chat Session", session)
	doc.check_permission("write")
	doc.db_set("permission_mode", mode)
	ran = []
	for name in frappe.get_all(
		"Chat Action", filters={"chat_session": session, "status": "Proposed", "draft": 0}, pluck="name"
	):
		action = frappe.get_doc("Chat Action", name)
		if allowed_without_asking(doc, {"routine": action.get("routine")}):
			ran.append({"name": name, **execute(name)})
	return {"mode": mode, "ran": ran}


def allowed_without_asking(session: Any, action: dict[str, Any]) -> bool:
	mode = session.get("permission_mode") or MODES[0]
	return mode == "Full access" or (mode == "Allow routine actions" and bool(action.get("routine")))


def execute(action: str) -> dict[str, Any]:
	doc = frappe.get_doc("Chat Action", action)
	payload = json.loads(str(doc.get("payload") or "{}"))
	session = frappe.get_doc("Chat Session", str(doc.get("chat_session")))
	pull_request = str(session.get("pull_request"))
	try:
		result = _run(str(doc.get("action_type")), payload, pull_request)
		doc.db_set({"status": "Executed", "result": result, "executed_by": frappe.session.user})
		return {"status": "Executed", "result": result}
	except (ProviderError, UnsupportedAction, frappe.ValidationError) as error:
		doc.db_set({"status": "Failed", "result": str(error)[:2000], "executed_by": frappe.session.user})
		return {"status": "Failed", "result": str(error)}


def _run(kind: str, payload: dict[str, Any], pull_request: str) -> str:
	if kind == "create_rule":
		full_name = db.value(
			"Code Repository", db.value("Pull Request", pull_request, "repository"), "full_name"
		)
		rule = frappe.get_doc(
			{
				"doctype": "Custom Context",
				"context_type": "Rule",
				"title": str(payload.get("body") or "")[:100],
				"body": payload.get("body") or "",
				"status": "Active",
				"source": "Chat",
				"repository_scope": full_name,
			}
		).insert()
		return f"Created rule {rule.name}"
	adapter, repo, number = _adapter(pull_request)
	body = str(payload.get("body") or "")
	comment = ProviderComment(
		str(payload.get("comment_id") or ""), "inline", "", "", thread_id=str(payload.get("comment_id") or "")
	)
	handlers = {
		"post_comment": lambda: adapter.post_comment(repo, number, body).url,
		"reply": lambda: adapter.reply_to_comment(repo, number, comment, body).url,
		"resolve_thread": lambda: adapter.resolve_thread(repo, number, comment.thread_id or ""),
		"reopen_thread": lambda: adapter.unresolve_thread(repo, number, comment.thread_id or ""),
		"request_review": lambda: adapter.request_reviewers(
			repo, number, list(payload.get("reviewers") or [])
		),
		"approve": lambda: adapter.approve(
			repo, number, str(db.value("Pull Request", pull_request, "head_sha")), body
		),
		"request_changes": lambda: adapter.request_changes(repo, number, body),
		"update_description": lambda: adapter.update_description(repo, number, body),
		"update_branch": lambda: adapter.update_branch(repo, number),
		"merge": lambda: adapter.merge(repo, number, str(payload.get("method") or "merge")),
		"close": lambda: adapter.close(repo, number),
		"reopen": lambda: adapter.reopen(repo, number),
		"draft": lambda: adapter.set_draft(repo, number, True),
		"ready": lambda: adapter.set_draft(repo, number, False),
	}
	if kind not in handlers:
		raise UnsupportedAction(kind)
	return str(handlers[kind]() or "done")


def _adapter(pull_request: str) -> tuple[Any, str, int]:
	from guard.providers import adapter_for_repository

	repository = str(db.value("Pull Request", pull_request, "repository"))
	adapter = adapter_for_repository(repository)
	if not adapter:
		raise frappe.ValidationError(_("The repository has no code provider."))
	user = db.value("Guard User", {"user": frappe.session.user}, "name")
	token = (
		frappe.get_doc("Guard User", str(user)).get_password("github_token", raise_exception=False)
		if user
		else None
	)
	if not token:
		raise frappe.ValidationError(
			_("Connect your GitHub account: add a GitHub token in Guard User. Actions run as you.")
		)
	return (
		adapter.with_token(str(token)),
		str(db.value("Code Repository", repository, "full_name")),
		int(db.value("Pull Request", pull_request, "number") or 0),
	)


def _session(pull_request: str, name: str | None) -> Any:
	if name:
		doc = frappe.get_doc("Chat Session", name)
		doc.check_permission("write")
		return doc
	return frappe.get_doc(
		{
			"doctype": "Chat Session",
			"kind": "Pull Request",
			"pull_request": pull_request,
			"user": frappe.session.user,
			"title": str(db.value("Pull Request", pull_request, "title"))[:120],
			"permission_mode": MODES[0],
		}
	).insert()


def _propose(session: Any, args: dict[str, Any]) -> dict[str, Any]:
	routine = args["action"] in ROUTINE
	draft = bool(args.get("draft"))
	doc = frappe.get_doc(
		{
			"doctype": "Chat Action",
			"chat_session": session.name,
			"action_type": args["action"],
			"summary": str(args.get("summary") or "")[:500],
			"payload": frappe.as_json(args),
			"status": "Proposed",
			"routine": int(routine),
			"draft": int(draft),
		}
	).insert()
	return {
		"name": doc.name,
		"action": args["action"],
		"summary": doc.get("summary"),
		"payload": args,
		"routine": routine,
		"draft": draft,
		"status": "Proposed",
	}
