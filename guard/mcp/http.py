"""HTTP of the MCP server: `POST /mcp` for JSON-RPC, and `POST /mcp/upload/<review id>` for diffs.

A page renderer serves both, so the address is short enough to type. The server is stateless: each
POST carries one JSON-RPC message, and the answer is one JSON document. There is no SSE stream, so
GET gets 405. The protocol checks and envelopes come from the MCP server of Frappe (`frappe.mcp`). The
tools and the instructions are Guard's.

Agents send an agent token from the Connect page (`guard.mcp.tokens`), or sign in with the OAuth of the
site. Both are OAuth bearer tokens: Frappe checks them before this module runs.
"""

from typing import Any

import frappe
from frappe.mcp import protocol, transport
from frappe.mcp.protocol import INVALID_REQUEST, METHOD_NOT_FOUND, McpError
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime, strip_html
from werkzeug.wrappers import Request, Response

import guard
from guard.mcp import prompts, tools
from guard.review import local
from guard.settings import public_url
from guard.utils import cache, db

MCP_PATH = "mcp"
UPLOAD_PATH = "mcp/upload/"
# Older revisions answer the same as the one Frappe calls legacy. A client gets the revision it asked for.
OLDER_VERSIONS = ("2025-03-26", "2025-06-18", protocol.LEGACY_VERSION)
CACHE_MS = 60 * 60 * 1000  # the tools change only with a deploy
SEEN_TTL = 90 * 24 * 60 * 60
UPLOAD_SECRET_HEADER = "X-Guard-Upload"
UPLOADS_PER_MINUTE = 60  # from one address; the upload has no user to count by


class Renderer:
	"""The page renderer of `/mcp` and `/mcp/upload/<review id>`."""

	def __init__(self, path: str, http_status_code: int | None = None):
		self.path = path

	def can_render(self) -> bool:
		return self.path == MCP_PATH or self.path.startswith(UPLOAD_PATH)

	def render(self) -> Response:
		if self.path == MCP_PATH:
			return serve(frappe.request)
		return upload(frappe.request, self.path.removeprefix(UPLOAD_PATH))


def serve(request: Request) -> Response:
	"""Answer one JSON-RPC message. A failure becomes a JSON-RPC error, never an HTML page."""
	frappe.local.mcp_request = transport.RequestContext()
	try:
		return _serve(request, frappe.local.mcp_request)
	except Exception as error:
		# The renderer answers, so Frappe commits: undo the writes of the failed call.
		db.rollback()
		if not isinstance(error, McpError | frappe.ValidationError | frappe.PermissionError):
			frappe.log_error("Guard MCP request failed")
		return transport.error_response(error)
	finally:
		# Frappe answers any later error of this request as MCP while this is set.
		frappe.local.mcp_request = None


def _serve(request: Request, context: transport.RequestContext) -> Response:
	if request.method != "POST":
		raise McpError(INVALID_REQUEST, "Only POST is supported", status=405)
	transport.check_origin(request)
	if not _token_session():
		raise McpError(
			INVALID_REQUEST,
			f"Guard needs an agent token. Get a setup prompt with one at {public_url('/desk/guard-agent')}.",
			status=401,
		)
	if not request.is_json:
		raise McpError(INVALID_REQUEST, "Content-Type must be application/json", status=415)
	if not request.accept_mimetypes["application/json"]:
		raise McpError(INVALID_REQUEST, "Accept must allow application/json", status=406)

	payload = transport.parse_body(request)
	context.id = payload.get("id")
	params = payload.get("params", {})
	requested = _version(request, params)
	context.version = (
		protocol.LATEST_VERSION if requested == protocol.LATEST_VERSION else protocol.LEGACY_VERSION
	)
	if context.version == protocol.LATEST_VERSION:
		transport.check_headers(request, payload, params)
		transport.check_metadata(params)
	if "id" not in payload:
		# A notification. Nothing is kept between requests.
		return Response(status=202)

	context.method = method = str(payload["method"])
	result = dispatch(method, params, context.version)
	return transport.json_response(protocol.result_envelope(payload["id"], result, context.version))


def dispatch(method: str, params: dict[str, Any], version: str) -> dict[str, Any]:
	if method == "initialize" and version != protocol.LATEST_VERSION:
		_seen(params.get("clientInfo"))
		requested = params.get("protocolVersion")
		return {
			"protocolVersion": requested if requested in OLDER_VERSIONS else protocol.LEGACY_VERSION,
			"capabilities": {"tools": {"listChanged": False}, "prompts": {"listChanged": False}},
			**_configuration(),
		}
	if method == "server/discover" and version == protocol.LATEST_VERSION:
		_seen((params.get("_meta") or {}).get(protocol.META_CLIENT))
		return {
			"supportedVersions": [protocol.LATEST_VERSION, *reversed(OLDER_VERSIONS)],
			"capabilities": {"tools": {}, "prompts": {}},
			**_configuration(),
			"ttlMs": CACHE_MS,
			"cacheScope": "private",
		}
	if method == "ping":
		return {}
	if method == "tools/list":
		return {"tools": tools.definitions(), "ttlMs": CACHE_MS, "cacheScope": "private"}
	if method == "prompts/list":
		return {"prompts": prompts.definitions(), "ttlMs": CACHE_MS, "cacheScope": "private"}
	if method == "prompts/get":
		return prompts.get(params)
	if method == "tools/call":
		_seen()
		return tools.call(params)
	raise McpError(METHOD_NOT_FOUND, f"Unknown MCP method: {method}", status=404)


def upload(request: Request, review_id: str) -> Response:
	"""Take the diff that the shell sends with curl. The answer is plain text for the shell."""
	if request.method != "POST":
		return _text(405, "error: POST the diff to this address.\n")
	try:
		answer = _receive(request, review_id)
	except Exception as error:
		db.rollback()
		status = getattr(error, "http_status_code", 500)
		if status >= 500:
			frappe.log_error("Guard upload of a local review failed")
		message = strip_html(str(error)).strip() if status < 500 else "Guard could not read the change."
		frappe.clear_messages()
		# 417 is the code of every ValidationError. 422 tells the shell that the input is wrong.
		return _text(422 if status == 417 else status, f"error: {message}\n")
	finally:
		# `receive` acts for the user who started the review. The session of this request stays Guest.
		frappe.set_user("Guest")
	return _text(
		200,
		f"Guard has the change: {answer['files']} files, {answer['findings']} findings of its checks.\n"
		f"Next: call the Guard tool review_context with review_id {review_id}.\n",
	)


@rate_limit(limit=UPLOADS_PER_MINUTE, seconds=60, ip_based=True)
def _receive(request: Request, review_id: str) -> dict[str, Any]:
	headers = request.headers
	return local.receive(
		review_id,
		headers.get(UPLOAD_SECRET_HEADER, ""),
		request.get_data,
		headers.get("X-Guard-Base", ""),
		headers.get("X-Guard-Branch", ""),
		headers.get("X-Guard-Head", ""),
	)


def _token_session() -> bool:
	"""True when a token in the `Authorization` header signed in the user. Frappe sets the sid to the user
	for a token, and to a hash for a browser session. A cookie alone is not enough: any page that the
	browser opens could then call the tools."""
	session = frappe.session
	return session.user not in (None, "", "Guest") and session.sid == session.user


def last_seen(user: str) -> dict[str, Any] | None:
	"""When the agent of `user` last called Guard, and which client it was."""
	return cache.get(_seen_key(user))


def _version(request: Request, params: dict[str, Any]) -> str:
	version = request.headers.get("MCP-Protocol-Version")
	meta = params.get("_meta")
	if not version:
		if isinstance(meta, dict) and meta.get(protocol.META_VERSION) == protocol.LATEST_VERSION:
			raise McpError(protocol.HEADER_MISMATCH, "MCP-Protocol-Version header is required")
		return protocol.LEGACY_VERSION
	if version != protocol.LATEST_VERSION and version not in OLDER_VERSIONS:
		raise McpError(
			protocol.UNSUPPORTED_VERSION,
			"Unsupported MCP protocol version",
			data={"supported": [protocol.LATEST_VERSION, *OLDER_VERSIONS], "requested": version},
		)
	return version


def _configuration() -> dict[str, Any]:
	return {
		"serverInfo": {"name": "guard", "title": "Guard", "version": guard.__version__},
		"instructions": tools.instructions(),
	}


def _seen(client: Any = None) -> None:
	"""Keep the time of the last call, so the Connect page can tell the developer that it works."""
	user = str(frappe.session.user)
	previous = last_seen(user) or {}
	name = client.get("name") if isinstance(client, dict) else None
	cache.set(
		_seen_key(user),
		{"at": now_datetime(), "client": str(name or previous.get("client") or "")[:100]},
		SEEN_TTL,
	)


def _seen_key(user: str) -> str:
	return f"guard:agent-seen:{user}"


def _text(status: int, body: str) -> Response:
	return Response(body, status=status, mimetype="text/plain")
