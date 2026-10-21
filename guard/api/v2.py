"""Query API: index a repository, read its status, ask questions, search code.

A repository id is `<remote>:<branch>:<owner/repo>`, for example `github:main:acme/api`.
"""

import json
from typing import Any

import frappe
from frappe import _
from werkzeug.wrappers import Response

from guard.conversation import codebase
from guard.indexing.jobs import enqueue_index, index_for
from guard.utils import db

STATUS = {
	"submitted": "submitted",
	"cloning": "cloning",
	"processing": "processing",
	"completed": "completed",
	"failed": "failed",
}


@frappe.whitelist(methods=["POST"])
def index_repository(
	repository: str,
	remote: str = "github",
	branch: str | None = None,
	reload: bool = False,
	notify: bool = False,
) -> dict[str, Any]:
	name = _repository(repository)
	frappe.has_permission("Code Repository", "write", name, throw=True)
	branch = branch or str(db.value("Code Repository", name, "default_branch"))
	enqueue_index(name, branch, full=bool(reload))
	repo_id = f"{remote}:{branch}:{repository}"
	return {
		"message": _("Indexing started"),
		"status_endpoint": f"/api/method/guard.api.v2.get_repository?repository_id={repo_id}",
	}


@frappe.whitelist(methods=["GET", "POST"])
def get_repository(repository_id: str) -> dict[str, Any]:
	remote, branch, full_name = parse_id(repository_id)
	name = _repository(full_name)
	frappe.has_permission("Code Repository", "read", name, throw=True)
	index = index_for(name, branch or None)
	row = db.values("Repository Index", index, ("status", "files_processed", "num_files", "sha")) or {}
	return {
		"repository": full_name,
		"remote": remote,
		"branch": branch,
		"private": bool(db.value("Code Repository", name, "is_private")),
		"status": STATUS.get(row.get("status") or "", "submitted"),
		"files_processed": row.get("files_processed") or 0,
		"num_files": row.get("num_files") or 0,
		"sha": row.get("sha") or "",
	}


@frappe.whitelist(methods=["POST"])
def query(
	messages: list[dict[str, str]],
	repositories: list[Any],
	session_id: str | None = None,
	stream: bool = False,
	genius: bool = False,
) -> Any:
	names = [_from_ref(ref) for ref in repositories]
	result = codebase.ask(names, messages, genius=bool(genius))
	if session_id:
		_save_session(session_id, names, messages, result.message, [s.as_dict() for s in result.sources])
	payload = {"message": result.message, "sources": [s.as_dict() for s in result.sources]}
	return _sse(payload) if stream else payload


@frappe.whitelist(methods=["POST"])
def search(query: str, repositories: list[Any], session_id: str | None = None, stream: bool = False) -> Any:
	names = [_from_ref(ref) for ref in repositories]
	payload = {"sources": [s.as_dict() for s in codebase.search(names, query)]}
	return _sse(payload) if stream else payload


def parse_id(repository_id: str) -> tuple[str, str, str]:
	parts = repository_id.split(":", 2)
	if len(parts) != 3:
		frappe.throw(_("A repository id is <remote>:<branch>:<owner/repo>."))
	return parts[0], parts[1], parts[2]


def _from_ref(ref: Any) -> str:
	if isinstance(ref, dict):
		full_name = ref.get("repository") or ref.get("name") or ""
	else:
		full_name = parse_id(str(ref))[2] if str(ref).count(":") >= 2 else str(ref)
	name = _repository(full_name)
	frappe.has_permission("Code Repository", "read", name, throw=True)
	return name


def _repository(full_name: str) -> str:
	name = db.value("Code Repository", {"full_name": full_name}, "name") or (
		full_name if frappe.db.exists("Code Repository", full_name) else None
	)
	if not name:
		frappe.throw(_("Unknown repository {0}").format(full_name), frappe.DoesNotExistError)
	return str(name)


def _save_session(
	key: str, repositories: list[str], messages: list[dict[str, str]], reply: str, sources: list[dict]
) -> None:
	name = db.value("Chat Session", {"session_key": key, "user": frappe.session.user}, "name")
	doc = (
		frappe.get_doc("Chat Session", str(name))
		if name
		else frappe.get_doc(
			{
				"doctype": "Chat Session",
				"kind": "API",
				"session_key": key,
				"user": frappe.session.user,
				"title": (messages[-1].get("content") or "")[:80],
			}
		)
	)
	doc.set("repositories", frappe.as_json(repositories))
	if messages:
		doc.append("messages", {"role": "user", "content": messages[-1].get("content") or ""})
	doc.append("messages", {"role": "assistant", "content": reply, "sources": frappe.as_json(sources)})
	doc.save(ignore_permissions=True)


def _sse(payload: dict[str, Any]) -> Response:
	events = [f"event: source\ndata: {json.dumps(s)}\n\n" for s in payload.get("sources") or []]
	if "message" in payload:
		events += [
			f"event: delta\ndata: {json.dumps({'text': chunk})}\n\n" for chunk in _chunks(payload["message"])
		]
	events.append(f"event: done\ndata: {json.dumps(payload)}\n\n")
	return Response("".join(events), mimetype="text/event-stream")


def _chunks(text: str, size: int = 200) -> list[str]:
	return [text[i : i + size] for i in range(0, len(text), size)] or [""]
