import json
from typing import Any

import frappe
from frappe.mcp import protocol
from werkzeug.datastructures import Headers
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request, Response

from guard.api import agent
from guard.mcp import http, tools
from guard.tests.test_local import LocalCase
from guard.utils import cache

ACCEPT = "application/json, text/event-stream"


def request(
	body: Any = None, method: str = "POST", headers: dict[str, str] | None = None, path: str = "/mcp"
) -> Request:
	builder = EnvironBuilder(
		path=path,
		method=method,
		json=body,
		headers=Headers({"Authorization": "Bearer token", "Accept": ACCEPT, **(headers or {})}),
	)
	return Request(builder.get_environ())


class TestMcpServer(LocalCase):
	def setUp(self):
		super().setUp()
		cache.delete(f"guard:agent-seen:{frappe.session.user}")

	def rpc(self, method: str, params: dict | None = None, headers: dict | None = None) -> dict:
		response = http.serve(
			request({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, headers=headers)
		)
		return json.loads(response.get_data())

	def call(self, tool: str, **arguments) -> dict:
		return self.rpc("tools/call", {"name": tool, "arguments": arguments})["result"]

	def upload(self, command: str, review_id: str, diff: str, secret: str | None = None) -> Response:
		secret = secret or command.split("X-Guard-Upload: ")[1].split("'")[0]
		builder = EnvironBuilder(
			path=f"/mcp/upload/{review_id}",
			method="POST",
			data=diff.encode(),
			headers=Headers(
				{
					"Content-Type": "application/octet-stream",
					http.UPLOAD_SECRET_HEADER: secret,
					"X-Guard-Base": self.base,
					"X-Guard-Branch": "feature",
				}
			),
		)
		frappe.set_user("Guest")
		try:
			return http.upload(Request(builder.get_environ()), review_id)
		finally:
			frappe.set_user("Administrator")

	def test_initialize_gives_the_workflow_and_the_revision_of_the_client(self):
		result = self.rpc(
			"initialize",
			{"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "claude-code"}},
		)["result"]
		self.assertEqual(result["protocolVersion"], "2025-06-18")
		self.assertEqual(result["serverInfo"]["name"], "guard")
		self.assertIn("start_review", result["instructions"])
		self.assertIn("Administrator", result["instructions"])
		self.assertEqual((http.last_seen("Administrator") or {})["client"], "claude-code")
		# An unknown revision gets the one that Frappe calls legacy.
		result = self.rpc("initialize", {"protocolVersion": "2030-01-01"})["result"]
		self.assertEqual(result["protocolVersion"], protocol.LEGACY_VERSION)

	def test_the_latest_revision_discovers_the_server(self):
		params = {
			"_meta": {
				protocol.META_VERSION: protocol.LATEST_VERSION,
				protocol.META_CAPABILITIES: {},
				protocol.META_CLIENT: {"name": "codex", "version": "1"},
			}
		}
		headers = {"MCP-Protocol-Version": protocol.LATEST_VERSION, "Mcp-Method": "server/discover"}
		result = self.rpc("server/discover", params, headers)["result"]
		self.assertIn("2025-06-18", result["supportedVersions"])
		self.assertIn("submit_review", result["instructions"])
		self.assertEqual(result["resultType"], "complete")

	def test_tools_list_names_the_tools_of_a_local_review(self):
		names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
		self.assertEqual(
			names, ["list_repositories", "start_review", "review_context", "find_symbol", "submit_review"]
		)

	def test_a_request_without_a_token_or_with_get_fails_as_json(self):
		# Frappe found no token, so the user is Guest.
		frappe.set_user("Guest")
		try:
			response = http.serve(request({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
		finally:
			frappe.set_user("Administrator")
		self.assertEqual(response.status_code, 401)
		self.assertIn("OAuth", json.loads(response.get_data())["error"]["message"])
		self.assertEqual(http.serve(request(method="GET")).status_code, 405)

	def test_a_review_from_start_to_submit(self):
		self.assertIn(self.full_name, self.call("list_repositories")["structuredContent"]["repositories"])
		started = self.call("start_review", repository=f"https://github.com/{self.full_name}.git")
		review_id = started["structuredContent"]["review_id"]
		command = started["structuredContent"]["command"]
		self.assertIn(f"/mcp/upload/{review_id}", command)
		self.assertIn("git merge-base HEAD origin/main", command)

		self.assertEqual(self.upload(command, review_id, self.working_tree_diff(), "wrong").status_code, 403)
		response = self.upload(command, review_id, self.working_tree_diff())
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertIn("review_context", response.get_data(as_text=True))
		self.assertEqual(self.upload(command, review_id, self.working_tree_diff()).status_code, 403)

		guide = self.call("review_context", review_id=review_id)["content"][0]["text"]
		self.assertIn("update_user_email", guide)
		submitted = self.call("submit_review", review_id=review_id, findings=[], summary="- Adds notify.")
		self.assertFalse(submitted["isError"])
		self.assertIn("/desk/code-review/", submitted["structuredContent"]["url"])
		self.assertTrue(http.last_seen("Administrator"))

	def test_a_failed_tool_tells_the_agent_what_to_do(self):
		result = self.call("review_context", review_id="nothing")
		self.assertTrue(result["isError"])
		self.assertIn("start_review", result["content"][0]["text"])
		result = self.call("start_review")
		self.assertEqual(result["content"][0]["text"], "start_review needs repository.")
		error = self.rpc("tools/call", {"name": "push", "arguments": {}})["error"]
		self.assertEqual(error["code"], protocol.INVALID_PARAMS)

	def test_a_browser_session_does_not_reach_the_tools(self):
		sid = frappe.session.sid
		frappe.session.sid = frappe.generate_hash()
		try:
			response = http.serve(request({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
		finally:
			frappe.session.sid = sid
		self.assertEqual(response.status_code, 401)

	def test_an_unknown_repository_is_a_clear_tool_error(self):
		result = self.call("find_symbol", repository="acme/nothing-here", name="x")
		self.assertTrue(result["isError"])
		self.assertIn("no repository acme/nothing-here", result["content"][0]["text"])

	def test_a_branch_that_is_unsafe_in_a_shell_gets_no_command(self):
		frappe.db.set_value("Code Repository", self.repository, "default_branch", "main;curl x|sh")
		result = self.call("start_review", repository=self.full_name)
		self.assertTrue(result["isError"])
		self.assertIn("safe command", result["content"][0]["text"])

	def test_a_small_diff_can_be_a_tool_argument(self):
		result = self.call(
			"start_review", repository=self.full_name, base=self.base, diff=self.working_tree_diff()
		)["structuredContent"]
		self.assertIn("update_user_email", result["guide"])

	def test_the_connect_page_gives_the_prompt_and_the_status(self):
		connect = agent.connect()
		self.assertIn(connect["endpoint"], connect["prompt"])
		self.assertTrue(connect["endpoint"].endswith("/mcp"))
		self.assertEqual(connect["oauth_off"], [])
		self.assertIsNone(connect["seen"])
		self.rpc("tools/call", {"name": "list_repositories", "arguments": {}})
		self.assertTrue(agent.connect()["seen"])

	def test_the_renderer_serves_only_its_paths(self):
		self.assertTrue(http.Renderer("mcp").can_render())
		self.assertTrue(http.Renderer("mcp/upload/abc").can_render())
		self.assertFalse(http.Renderer("mcpx").can_render())
		self.assertEqual(len(tools.definitions()), len(tools.TOOLS))
