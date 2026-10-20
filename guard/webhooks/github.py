"""GitHub webhook parsing."""

import hashlib
import hmac
from typing import Any

from guard.providers.base import ProviderComment, RepositoryInfo
from guard.providers.github import pull_request_info
from guard.webhooks.events import IGNORED, WebhookEvent

OPEN_ACTIONS = {"opened": "opened", "reopened": "reopened", "ready_for_review": "ready"}


def verify(headers: dict[str, str], body: bytes, secret: str) -> bool:
	signature = headers.get("X-Hub-Signature-256") or ""
	expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
	return hmac.compare_digest(signature.removeprefix("sha256="), expected)


def delivery_id(headers: dict[str, str]) -> str:
	return headers.get("X-GitHub-Delivery") or ""


def event_name(headers: dict[str, str]) -> str:
	return headers.get("X-GitHub-Event") or ""


def parse(event: str, payload: dict[str, Any]) -> list[WebhookEvent]:
	parser = PARSERS.get(event)
	return parser(payload) if parser else [IGNORED]


def repository(data: dict[str, Any] | None) -> RepositoryInfo | None:
	if not data:
		return None
	return RepositoryInfo(
		full_name=data["full_name"],
		provider_repo_id=str(data.get("id") or ""),
		default_branch=data.get("default_branch") or "main",
		clone_url=data.get("clone_url") or "",
		web_url=data.get("html_url") or "",
		is_private=bool(data.get("private")),
	)


def _base(payload: dict[str, Any], kind: str, action: str) -> WebhookEvent:
	repo = repository(payload.get("repository"))
	owner = ((payload.get("repository") or {}).get("owner") or {}).get("login") or ""
	return WebhookEvent(
		kind=kind,
		action=action,
		repository=repo,
		namespace=(payload.get("organization") or {}).get("login") or owner,
		sender=(payload.get("sender") or {}).get("login") or "",
		installation_id=str((payload.get("installation") or {}).get("id") or "") or None,
	)


def _pull_request(payload: dict[str, Any]) -> list[WebhookEvent]:
	action = payload.get("action") or ""
	data = payload.get("pull_request") or {}
	event = _base(payload, "pull_request", OPEN_ACTIONS.get(action, action))
	event.number = int(data.get("number") or payload.get("number") or 0)
	event.pull_request = pull_request_info(data) if data else None
	if action == "synchronize":
		event.before = payload.get("before")
		event.after = payload.get("after") or (data.get("head") or {}).get("sha")
	if action == "labeled":
		event.label = (payload.get("label") or {}).get("name")
	if action == "closed":
		event.action = "merged" if data.get("merged") else "closed"
	return [event]


def _issue_comment(payload: dict[str, Any]) -> list[WebhookEvent]:
	issue = payload.get("issue") or {}
	if not issue.get("pull_request"):
		return [IGNORED]
	data = payload.get("comment") or {}
	event = _base(payload, "comment", payload.get("action") or "")
	event.number = int(issue.get("number") or 0)
	event.comment = ProviderComment(
		id=str(data.get("id")),
		kind="issue",
		author=(data.get("user") or {}).get("login") or "",
		body=data.get("body") or "",
		created_at=data.get("created_at") or "",
		is_bot=(data.get("user") or {}).get("type") == "Bot",
		number=event.number,
	)
	return [event]


def _review_comment(payload: dict[str, Any]) -> list[WebhookEvent]:
	data = payload.get("comment") or {}
	pull = payload.get("pull_request") or {}
	event = _base(payload, "comment", payload.get("action") or "")
	event.number = int(pull.get("number") or 0)
	reply_to = data.get("in_reply_to_id")
	event.comment = ProviderComment(
		id=str(data.get("id")),
		kind="inline",
		author=(data.get("user") or {}).get("login") or "",
		body=data.get("body") or "",
		created_at=data.get("created_at") or "",
		path=data.get("path"),
		line=data.get("line") or data.get("original_line"),
		in_reply_to=str(reply_to) if reply_to else None,
		thread_id=str(reply_to or data.get("id")),
		is_bot=(data.get("user") or {}).get("type") == "Bot",
		number=event.number,
	)
	return [event]


def _review(payload: dict[str, Any]) -> list[WebhookEvent]:
	review = payload.get("review") or {}
	if not review.get("body"):
		return [IGNORED]
	pull = payload.get("pull_request") or {}
	event = _base(payload, "comment", "created")
	event.number = int(pull.get("number") or 0)
	event.comment = ProviderComment(
		id=str(review.get("id")),
		kind="review",
		author=(review.get("user") or {}).get("login") or "",
		body=review.get("body") or "",
		created_at=review.get("submitted_at") or "",
		number=event.number,
	)
	return [event]


def _push(payload: dict[str, Any]) -> list[WebhookEvent]:
	event = _base(payload, "push", "push")
	event.ref = (payload.get("ref") or "").removeprefix("refs/heads/")
	event.before, event.after = payload.get("before"), payload.get("after")
	return [event]


def _installation(payload: dict[str, Any]) -> list[WebhookEvent]:
	installation = payload.get("installation") or {}
	event = WebhookEvent(kind="installation", action=payload.get("action") or "")
	event.installation_id = str(installation.get("id") or "")
	event.namespace = (installation.get("account") or {}).get("login") or ""
	added = payload.get("repositories_added") or payload.get("repositories") or []
	event.repositories = [
		RepositoryInfo(
			full_name=r["full_name"],
			provider_repo_id=str(r.get("id") or ""),
			default_branch="",
			clone_url="",
			web_url="",
			is_private=bool(r.get("private")),
		)
		for r in added
	]
	return [event]


def _check_run(payload: dict[str, Any]) -> list[WebhookEvent]:
	if payload.get("action") != "rerequested":
		return [IGNORED]
	run = payload.get("check_run") or {}
	pulls = run.get("pull_requests") or []
	if not pulls:
		return [IGNORED]
	event = _base(payload, "rerun", "rerequested")
	event.number = int(pulls[0]["number"])
	return [event]


PARSERS = {
	"pull_request": _pull_request,
	"issue_comment": _issue_comment,
	"pull_request_review_comment": _review_comment,
	"pull_request_review": _review,
	"push": _push,
	"installation": _installation,
	"installation_repositories": _installation,
	"check_run": _check_run,
}
