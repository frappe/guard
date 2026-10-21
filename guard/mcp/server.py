"""MCP over streamable HTTP, JSON response mode. One POST is one JSON-RPC message or batch."""

import json
from typing import Any

import frappe
from werkzeug.wrappers import Response

from guard.mcp.tools import TOOLS
from guard.utils.redact import redact

PROTOCOL = "2025-06-18"
SERVER = {"name": "guard", "version": "1.0.0"}


@frappe.whitelist(methods=["POST"])
def handle(**kwargs: Any) -> Response:
	if frappe.session.user == "Guest":
		return _json(
			{"jsonrpc": "2.0", "id": None, "error": {"code": -32000, "message": "Authentication required"}},
			401,
		)
	try:
		body = json.loads(frappe.request.get_data() or b"{}")
	except json.JSONDecodeError:
		return _json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
	if isinstance(body, list):
		replies = [r for r in (dispatch(m) for m in body) if r is not None]
		return _json(replies) if replies else Response(status=202)
	reply = dispatch(body)
	return _json(reply) if reply is not None else Response(status=202)


def dispatch(message: Any) -> dict[str, Any] | None:
	if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
		return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}}
	method, mid = message.get("method"), message.get("id")
	if mid is None:  # notification
		return None
	params = message.get("params") or {}
	if method == "initialize":
		return _ok(
			mid,
			{
				"protocolVersion": params.get("protocolVersion") or PROTOCOL,
				"capabilities": {"tools": {"listChanged": False}},
				"serverInfo": SERVER,
			},
		)
	if method == "ping":
		return _ok(mid, {})
	if method == "tools/list":
		return _ok(
			mid,
			{
				"tools": [
					{"name": t.name, "description": t.description, "inputSchema": t.schema}
					for t in TOOLS.values()
				]
			},
		)
	if method == "tools/call":
		return _ok(mid, call(params.get("name"), params.get("arguments") or {}))
	return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "Method not found"}}


def call(name: str | None, arguments: dict[str, Any]) -> dict[str, Any]:
	tool = TOOLS.get(name or "")
	if not tool:
		return _error_result(f"Unknown tool {name}")
	try:
		result = tool.handler(**arguments)
	except TypeError as error:
		return _error_result(f"Invalid params: {error}")
	except (frappe.PermissionError, frappe.DoesNotExistError, ValueError) as error:
		frappe.db.rollback()
		return _error_result(str(error) or type(error).__name__)
	except Exception as error:
		frappe.db.rollback()
		frappe.log_error(redact(str(error)), "Guard MCP")
		return _error_result("An internal error occurred while processing your request")
	text = json.dumps(result, default=str)
	return {
		"content": [{"type": "text", "text": text}],
		"structuredContent": result if isinstance(result, dict) else {"result": result},
		"isError": False,
	}


def _error_result(message: str) -> dict[str, Any]:
	return {"content": [{"type": "text", "text": message}], "isError": True}


def _ok(mid: Any, result: dict[str, Any]) -> dict[str, Any]:
	return {"jsonrpc": "2.0", "id": mid, "result": result}


def _json(payload: Any, status: int = 200) -> Response:
	return Response(json.dumps(payload, default=str), status=status, mimetype="application/json")
