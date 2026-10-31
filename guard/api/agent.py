"""The Connect Your Agent page: one prompt that connects the coding agent of the developer to Guard."""

from typing import Any

import frappe

from guard.mcp import tokens
from guard.mcp.http import last_seen
from guard.settings import public_url

RECENT_REVIEWS = 5

# One prompt for every client: the agent knows which client it runs in. The client facts are the usual
# setup mistakes, so keep them.
SETUP_PROMPT = """Connect this coding agent to Guard, the code reviewer of my team. Do it at user scope, so it works in all my repositories.
  Name: guard
  URL: {endpoint}
  Transport: streamable HTTP
  Header: Authorization: Bearer {token}
Use the command of this client when it has one:
  Claude Code: claude mcp add --transport http --scope user guard {endpoint} --header "Authorization: Bearer {token}"
  pi: pi mcp add guard --url {endpoint} --header "Authorization=Bearer {token}" --exposure direct
  Codex: add to ~/.codex/config.toml (keep the rest of the file):
    [mcp_servers.guard]
    url = "{endpoint}"
    http_headers = {{ "Authorization" = "Bearer {token}" }}
Otherwise merge an entry into the MCP config file of this client. Do not overwrite the file, or I lose my other MCP servers. The top-level key differs per client: `servers` in VS Code, `mcp` in OpenCode, `mcpServers` in the others.
The token is a password for Guard. Put it only in the MCP config of this client: not in a repository, a shell profile or a reply to me.
Then call the Guard tool list_repositories (restart or reload the MCP servers first if this client needs it) and tell me what it returned. Then tell me in a few lines what I can ask you to do with Guard."""


@frappe.whitelist(methods=["GET"])
def connect() -> dict[str, Any]:
	"""The address of the MCP server, the agent tokens of the user, and when their agent last called."""
	frappe.has_permission("Code Repository", "read", throw=True)
	user = str(frappe.session.user)
	return {
		"endpoint": public_url("/mcp"),
		"tokens": tokens.active(user),
		"seen": last_seen(user),
		"reviews": frappe.get_list(
			"Code Review",
			filters={"source": "local", "requested_by": user},
			fields=["name", "repository", "branch", "findings_count", "confidence_score", "creation"],
			order_by="creation desc",
			limit=RECENT_REVIEWS,
		),
	}


@frappe.whitelist(methods=["POST"])
def new_token() -> dict[str, str]:
	"""A new agent token in the setup prompt. The page shows it once. `token` is the name of the record,
	so the page can drop the prompt when the token is revoked."""
	frappe.has_permission("Code Repository", "read", throw=True)
	user = str(frappe.session.user)
	token = tokens.issue(user)
	return {
		"prompt": SETUP_PROMPT.format(endpoint=public_url("/mcp"), token=token),
		"token": tokens.active(user)[0]["name"],
	}


@frappe.whitelist(methods=["POST"])
def revoke_token(name: str) -> None:
	tokens.revoke(str(frappe.session.user), name)
