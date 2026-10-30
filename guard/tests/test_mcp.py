import io
import json
import os
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import frappe
from frappe.mcp import protocol
from werkzeug.datastructures import Headers
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request, Response

from guard.api import agent
from guard.llm.embeddings import Embedder
from guard.mcp import http, kit, prompts, tools
from guard.tests.test_learning import FINDING, LearningCase
from guard.tests.test_local import NEW_SIGNATURE, LocalCase
from guard.utils import cache, db

# LocalCase stops embeddings. A code search embeds its query, as the index did.
EMBED = Embedder.embed
# A curl that keeps the body it would send, and prints the headers.
FAKE_CURL = """#!/bin/sh
for a; do case $a in @*) cp "${a#@}" "$GUARD_TEST_BODY" ;; X-Guard-*) echo "$a" ;; esac; done
"""
# For the scripts: a curl that serves the kit and logs uploads, and agents that log their arguments.
STUB_CURL = """#!/bin/sh
for a; do case $a in
*/mcp/kit.tar.gz) cat "$GUARD_TEST_KIT"; exit 0 ;;
@*) cp "${a#@}" "$GUARD_TEST_BODY" ;;
http*) echo "curl $a" >> "$GUARD_TEST_LOG" ;;
esac; done
exit "${GUARD_TEST_CURL:-0}"
"""
STUB_AGENT = """#!/bin/sh
echo "$(basename "$0") $*" >> "$GUARD_TEST_LOG"
case "$*" in
"mcp get"*) exit 1 ;;
*start_review*) printf 'GUARD_REVIEW=%s\\nGUARD_SECRET=%s\\nGUARD_BRANCH=main\\n' "$GUARD_TEST_ID" "$GUARD_TEST_SECRET" ;;
*review_context*) printf 'A P0 in app/models.py:3.\\nGUARD_P0=%s\\nGUARD_URL=https://x.test/r\\n' "$GUARD_TEST_P0" ;;
esac
"""
REVIEW_ID, SECRET = "0123456789abcdef", "0123456789abcdef" * 2

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

	def test_tools_list_names_the_tools(self):
		names = [t["name"] for t in self.rpc("tools/list")["result"]["tools"]]
		self.assertEqual(
			names,
			[
				"list_repositories",
				"start_review",
				"review_context",
				"find_symbol",
				"submit_review",
				"pr_findings",
				"finding_feedback",
				"search_code",
				"team_rules",
				"explain_config",
				"request_review",
			],
		)

	def test_prompts_start_the_tasks(self):
		result = self.rpc("initialize", {"protocolVersion": "2025-06-18"})["result"]
		self.assertIn("prompts", result["capabilities"])
		names = [p["name"] for p in self.rpc("prompts/list")["result"]["prompts"]]
		self.assertEqual(names, list(prompts.PROMPTS))
		got = self.rpc("prompts/get", {"name": "review", "arguments": {"focus": "the SQL"}})["result"]
		text = got["messages"][0]["content"]["text"]
		self.assertIn("start_review", text)
		self.assertTrue(text.endswith("Focus on: the SQL"))
		error = self.rpc("prompts/get", {"name": "ask", "arguments": {}})["error"]
		self.assertEqual(error["code"], protocol.INVALID_PARAMS)

	def test_the_upload_command_sends_new_files_and_keeps_the_index(self):
		self.repo.git("update-ref", "refs/remotes/origin/main", self.base)
		(self.repo.path / "app/models.py").write_text(NEW_SIGNATURE)
		(self.repo.path / "app/notes.py").write_text("NOTE = 1\n")
		(self.repo.path / "app/.env.local").write_text("TOKEN=x\n")
		started = self.call("start_review", repository=self.full_name)["structuredContent"]
		self.assertEqual(started["upload"]["secret"], started["command"].split("X-Guard-Upload: ")[1][:32])
		with tempfile.TemporaryDirectory() as bin_dir:
			curl = Path(bin_dir) / "curl"
			curl.write_text(FAKE_CURL)
			curl.chmod(0o755)
			body = Path(bin_dir) / "body"
			env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "GUARD_TEST_BODY": str(body)}
			result = subprocess.run(
				["sh", "-c", started["command"]],
				cwd=self.repo.path / "app",
				env=env,
				capture_output=True,
				text=True,
			)
			self.assertEqual(result.returncode, 0, result.stderr)
			self.assertIn(f"X-Guard-Base: {self.base}", result.stdout)
			diff = body.read_text()
		self.assertIn("+++ b/app/notes.py", diff)
		self.assertNotIn(".env.local", diff)
		self.assertIn("notify", diff)
		# The new file is still untracked: the command used a copy of the index.
		self.assertEqual(self.repo.git("status", "--porcelain", "app/notes.py"), "?? app/notes.py")
		response = self.upload(started["command"], started["review_id"], diff)
		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))

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
		for path in ("mcp", "mcp/upload/abc", "mcp/install.sh", "mcp/kit.tar.gz"):
			self.assertTrue(http.Renderer(path).can_render(), path)
		self.assertFalse(http.Renderer("mcpx").can_render())
		self.assertFalse(http.Renderer("mcp/other").can_render())
		self.assertEqual(len(tools.definitions()), len(tools.TOOLS))

	def test_the_install_script_has_the_site_and_the_kit_has_the_skills(self):
		script = kit.install_script()
		self.assertNotIn(kit.SITE_MARK, script)
		self.assertIn(f'SITE="{agent.public_url("")}"', script)
		self.assertTrue(agent.connect()["install"].endswith("/mcp/install.sh | sh -s -- --all"))
		self.assertIn("--all --hook --no-login", agent.connect()["install_hook"])
		with tarfile.open(fileobj=io.BytesIO(kit.archive())) as tar:
			members = {m.name: m for m in tar.getmembers()}
			self.assertEqual(members["pre-push"].mode, kit.EXECUTABLE)
			for skill in ("guard-review", "guard-fix", "guard-feedback", "guard-ask"):
				self.assertIn(skill, script)
				text = (tar.extractfile(f"skills/{skill}/SKILL.md") or io.BytesIO()).read().decode()
				self.assertTrue(text.startswith(f"---\nname: {skill}\ndescription: "), skill)
		self.assertEqual(kit.archive(), kit.archive())

	def test_a_public_url_with_shell_characters_or_plain_http_gets_no_script(self):
		for url in ("https://x.test/$(id)", "http://guard.example.com"):
			with patch("guard.mcp.kit.public_url", return_value=url):
				with self.assertRaises(frappe.ValidationError):
					kit.install_script()
		with patch("guard.mcp.kit.public_url", return_value="http://guard.localhost:8000"):
			self.assertIn('SITE="http://guard.localhost:8000"', kit.install_script())

	def test_find_symbol_without_a_repository_looks_in_all(self):
		found = self.call("find_symbol", name="update_user_email")["structuredContent"]
		self.assertIn(self.full_name, [d["repository"] for d in found["definitions"]])
		users = [(u["repository"], u["path"]) for u in found["users"]]
		self.assertIn((self.full_name, "app/service.py"), users)

	def test_search_rules_and_config(self):
		with patch("guard.llm.embeddings.Embedder.embed", EMBED):
			result = self.call("search_code", query="update_user_email", repositories=[self.full_name])
		self.assertFalse(result["isError"], result["content"])
		self.assertIn("app/models.py", [r["filepath"] for r in result["structuredContent"]["results"]])
		frappe.get_doc(
			{
				"doctype": "Custom Context",
				"context_type": "Pattern",
				"status": "Active",
				"title": "User dicts",
				"body": "User dicts are copies for one request.",
				"path_scope": "app/**",
			}
		).insert()
		rules = self.call("team_rules", repository=self.full_name, path="app/models.py")["structuredContent"]
		self.assertTrue(any("copies for one request" in m for m in rules["memories"]))
		config = self.call("explain_config", repository=self.full_name, path="app/models.py")
		self.assertIn("settings", config["structuredContent"])


class TestPullRequestTools(LearningCase):
	"""The tools for the findings of a pull request, as the developer who signed in."""

	def setUp(self):
		super().setUp()
		self.full_name = str(db.value("Code Repository", self.repository, "full_name"))

	def call(self, tool: str, **arguments) -> dict:
		return tools.call({"name": tool, "arguments": arguments})

	def test_pr_findings_of_the_branch(self):
		finding, _thread = self.posted_finding()
		result = self.call("pr_findings", repository=self.full_name, branch="feature")
		self.assertFalse(result["isError"], result["content"])
		found = result["structuredContent"]
		self.assertEqual(found["pull_request"]["number"], 7)
		self.assertEqual([f["finding"] for f in found["findings"]], [finding])
		self.assertEqual(found["findings"][0]["title"], FINDING["title"])
		self.assertIn(f"/desk/review-finding/{finding}", found["findings"][0]["url"])
		self.assertEqual(
			self.call("pr_findings", repository=self.full_name, number=7)["structuredContent"]["findings"][0][
				"finding"
			],
			finding,
		)
		missing = self.call("pr_findings", repository=self.full_name, branch="other")
		self.assertTrue(missing["isError"])
		self.assertIn("start_review", missing["content"][0]["text"])

	def test_a_verdict_dismisses_the_finding_and_teaches_guard(self):
		finding, _thread = self.posted_finding()
		short = self.call("finding_feedback", finding=finding, verdict="wrong", reason="no")
		self.assertTrue(short["isError"])
		answer = self.call(
			"finding_feedback",
			finding=finding,
			verdict="intended",
			reason="User dicts are copies for one request.",
		)
		self.assertFalse(answer["isError"], answer["content"])
		self.assertEqual(db.value("Review Finding", finding, "status"), "Dismissed")
		feedback = (
			db.values("Review Feedback", {"finding": finding, "kind": "Reply"}, ("sentiment", "body")) or {}
		)
		self.assertEqual(feedback["sentiment"], "Context")
		self.assertEqual(db.value("Review Finding", finding, "downvotes"), 1)
		# A new reason takes the place of the first one, and counts once.
		again = self.call(
			"finding_feedback", finding=finding, verdict="wrong", reason="User dicts are copies, again."
		)
		self.assertIn("new reason", again["structuredContent"]["learning"])
		self.assertEqual(frappe.db.count("Review Feedback", {"finding": finding, "kind": "Reply"}), 1)
		self.assertEqual(db.value("Review Finding", finding, "downvotes"), 1)
		self.call("finding_feedback", finding=finding, verdict="reopen")
		self.assertEqual(db.value("Review Finding", finding, "status"), "Open")

	def test_a_user_who_cannot_teach_dismisses_but_does_not_teach(self):
		finding, _thread = self.posted_finding()
		frappe.db.set_single_value("Guard Settings", "teach_permission", "Guard users")
		answer = self.call(
			"finding_feedback", finding=finding, verdict="wrong", reason="This is not a bug at all."
		)
		self.assertEqual(answer["structuredContent"]["status"], "Dismissed")
		self.assertIn("does not learn", answer["structuredContent"]["learning"])
		self.assertFalse(frappe.db.exists("Review Feedback", {"finding": finding, "kind": "Reply"}))

	def test_a_branch_of_a_fork_is_not_the_pull_request(self):
		frappe.db.set_value("Pull Request", self.pr, "head_clone_url", "https://github.com/someone/fork.git")
		self.assertTrue(self.call("pr_findings", repository=self.full_name, branch="feature")["isError"])
		self.assertFalse(self.call("pr_findings", repository=self.full_name, number=7)["isError"])

	def test_request_review_queues_a_review_of_the_pull_request(self):
		with patch("guard.review.jobs.create_review", return_value="CR-1") as create:
			result = self.call("request_review", repository=self.full_name, branch="feature", tier="plus")
		self.assertFalse(result["isError"], result["content"])
		self.assertEqual(create.call_args.args[:2], (self.pr, "manual"))
		self.assertEqual(create.call_args.kwargs["requested_tier"], "plus")
		self.assertTrue(
			self.call("request_review", repository=self.full_name, number=7, tier="max")["isError"]
		)


class TestScripts(LocalCase):
	"""The install script and the pre-push hook, with stub agents and a stub curl."""

	def setUp(self):
		super().setUp()
		self.tmp = Path(tempfile.mkdtemp())
		self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
		self.bin, self.home = self.tmp / "bin", self.tmp / "home"
		self.bin.mkdir()
		self.home.mkdir()
		for name, text in (
			("curl", STUB_CURL),
			("claude", STUB_AGENT),
			("pi", STUB_AGENT),
			("codex", STUB_AGENT),
		):
			(self.bin / name).write_text(text)
			(self.bin / name).chmod(0o755)
		(self.tmp / "kit.tar.gz").write_bytes(kit.archive())
		self.log = self.tmp / "log"
		self.log.touch()

	def sh(self, script: str, *args: str, stdin: str = "", **env: str) -> subprocess.CompletedProcess:
		environ = {
			# No /usr/local or ~/.local: only the stubs are agents.
			"PATH": f"{self.bin}:/usr/bin:/bin",
			"HOME": str(self.home),
			"GUARD_TEST_LOG": str(self.log),
			"GUARD_TEST_KIT": str(self.tmp / "kit.tar.gz"),
			"GUARD_TEST_BODY": str(self.tmp / "body"),
			"GUARD_TEST_ID": REVIEW_ID,
			"GUARD_TEST_SECRET": SECRET,
			**env,
		}
		# A new session has no terminal, so the script asks nothing.
		return subprocess.run(
			["sh", "-c", script, "sh", *args],
			cwd=self.repo.path,
			env=environ,
			input=stdin,
			capture_output=True,
			text=True,
			start_new_session=True,
		)

	def test_the_install_script_sets_up_each_agent_once(self):
		script = kit.install_script()
		dry = self.sh(script, "--all", "--dry-run", "--no-hook")
		self.assertEqual(dry.returncode, 0, dry.stderr)
		self.assertIn(
			f"would run: claude mcp add --transport http --scope user guard {agent.public_url('/mcp')}",
			dry.stdout,
		)
		self.assertIn("would run: pi mcp add guard", dry.stdout)
		self.assertEqual(list(self.home.iterdir()), [])
		no_choice = self.sh(script, "--dry-run", "--no-hook")
		self.assertNotEqual(no_choice.returncode, 0)
		self.assertIn("no terminal prompt is available", no_choice.stderr)

		claude_only = self.sh(script, "--client", "claude", "--dry-run", "--no-hook")
		self.assertEqual(claude_only.returncode, 0, claude_only.stderr)
		self.assertIn("would run: claude mcp add", claude_only.stdout)
		self.assertNotIn("Codex", claude_only.stdout)
		self.assertNotIn("pi mcp add", claude_only.stdout)

		all_agents = self.sh(script, "--all", "--dry-run", "--no-hook")
		self.assertEqual(all_agents.returncode, 0, all_agents.stderr)
		self.assertIn("would run: claude mcp add", all_agents.stdout)
		self.assertIn("would add [mcp_servers.guard]", all_agents.stdout)
		self.assertIn("would run: pi mcp add", all_agents.stdout)

		(self.bin / "codex").unlink()
		without_codex = self.sh(script, "--all", "--dry-run", "--no-hook")
		self.assertEqual(without_codex.returncode, 0, without_codex.stderr)
		self.assertNotIn("Codex", without_codex.stdout)
		missing = self.sh(script, "--client", "codex", "--dry-run", "--no-hook")
		self.assertIn("codex is not installed", missing.stderr)
		(self.bin / "codex").write_text(STUB_AGENT)
		(self.bin / "codex").chmod(0o755)

		first = self.sh(script, "--all", "--hook")
		self.assertEqual(first.returncode, 0, first.stderr)
		for folder in (".claude/skills", ".agents/skills"):
			self.assertTrue((self.home / folder / "guard-review" / "SKILL.md").is_file(), folder)
		config = (self.home / ".codex/config.toml").read_text()
		self.assertIn(f'url = "{agent.public_url("/mcp")}"', config)
		hook = self.repo.path / ".git/hooks/pre-push"
		self.assertIn(agent.public_url(""), hook.read_text())
		self.assertTrue(os.access(hook, os.X_OK))
		self.assertIn("codex mcp login guard", first.stdout)

		again = self.sh(script, "--all", "--no-hook")
		self.assertIn("MCP server guard: already set up", again.stdout)
		self.assertEqual((self.home / ".codex/config.toml").read_text(), config)

		removed = self.sh(script, "--uninstall")
		self.assertEqual(removed.returncode, 0, removed.stderr)
		self.assertFalse(hook.exists())
		self.assertFalse((self.home / ".claude/skills/guard-review").exists())
		self.assertIn("claude mcp remove guard --scope user", self.log.read_text())

	def hook(self, **env: str) -> subprocess.CompletedProcess:
		if not getattr(self, "head", None):
			self.repo.git("update-ref", "refs/remotes/origin/main", self.base)
			self.repo.branch_off("feature")
			self.head = self.repo.commit({"app/models.py": NEW_SIGNATURE}, "add notify")
		head = self.head
		script = (
			tarfile.open(fileobj=io.BytesIO(kit.archive())).extractfile("pre-push") or io.BytesIO()
		).read()
		lines = f"refs/tags/v1 {head} refs/tags/v1 {'0' * 40}\nrefs/heads/feature {head} refs/heads/feature {'0' * 40}\n"
		return self.sh(script.decode(), "origin", "https://github.com/acme/app.git", stdin=lines, **env)

	def test_the_hook_stops_a_push_with_a_p0_and_gives_the_agent_no_shell(self):
		result = self.hook(GUARD_TEST_P0="2")
		self.assertEqual(result.returncode, 1, result.stderr)
		self.assertIn("push stopped: 2 P0 findings. https://x.test/r", result.stderr)
		log = self.log.read_text()
		self.assertIn(f"curl {agent.public_url('/mcp/upload/' + REVIEW_ID)}", log)
		calls = log.split("claude -p ")[1:]
		self.assertEqual(len(calls), 2)
		self.assertIn("--allowedTools mcp__guard --disallowedTools Bash", calls[0])
		self.assertIn("--allowedTools mcp__guard,Read,Grep,Glob --disallowedTools Bash", calls[1])
		self.assertIn(f"review_id {REVIEW_ID}", calls[1])
		# Without an origin remote, the repository is the address of the push.
		self.assertIn("start_review with repository https://github.com/acme/app.git.", calls[0])
		# The hook sends the pushed commits, not the working tree.
		self.assertIn("+def update_user_email(user, email, notify):", (self.tmp / "body").read_text())

	def test_the_hook_lets_the_push_go_when_the_review_does_not_finish(self):
		self.assertEqual(self.hook(GUARD_TEST_P0="0").returncode, 0)
		failed = self.hook(GUARD_TEST_P0="0", GUARD_TEST_CURL="22")
		self.assertEqual(failed.returncode, 0)
		self.assertIn("Guard did not take the diff", failed.stderr)
		bad = self.hook(GUARD_TEST_ID="x;rm -rf /")
		self.assertEqual(bad.returncode, 0)
		self.assertIn("did not start a Guard review", bad.stderr)
		self.assertEqual(self.hook(GUARD_SKIP="1").returncode, 0)
