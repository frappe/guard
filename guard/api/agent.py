"""The Connect Your Agent page: how a developer adds the MCP server of Guard to their coding agent."""

from typing import Any

import frappe
from frappe import _

from guard.mcp.http import last_seen
from guard.mcp.kit import install_command
from guard.settings import public_url

RECENT_REVIEWS = 5

# One prompt for every client: the agent knows which client it runs in. The client facts in the second
# paragraph are the usual setup mistakes, so keep them.
SETUP_PROMPT = """Add the Guard MCP server to this client, at user scope, so it works in all my repositories.
  Name: guard
  URL: {endpoint}
  Transport: streamable HTTP
  Sign-in: OAuth. The client registers itself. Do not add a header or a token.
Use the command of this client when it has one (Claude Code: `claude mcp add --transport http --scope user guard {endpoint}`, Codex: `codex mcp add guard --url {endpoint}`). Otherwise merge an entry into its MCP config file. Do not overwrite the file, or I lose my other MCP servers. The top-level key differs per client: `servers` in VS Code, `mcp` in OpenCode, `mcp_servers` in Codex, `mcpServers` in the others.
Then tell me how to sign in to Guard from this client (Claude Code: run /mcp, select guard, then Authenticate; Codex: `codex mcp login guard`). Restart the client if it reads its config only at start.
When I have signed in, call the Guard tool list_repositories and tell me what it returned. Then tell me in a few lines how I ask you for a Guard review before I push."""

# OAuth Settings that an MCP client needs to sign in by itself. All are on by default.
OAUTH_FIELDS = {
	"show_protected_resource_metadata": "Show Protected Resource Metadata",
	"show_auth_server_metadata": "Show Auth Server Metadata",
	"enable_dynamic_client_registration": "Enable Dynamic Client Registration",
}


@frappe.whitelist(methods=["GET", "POST"])
def connect() -> dict[str, Any]:
	"""The install commands, the address of the MCP server, the setup prompt for other clients, and when
	the agent of the user last called."""
	frappe.has_permission("Code Repository", "read", throw=True)
	endpoint = public_url("/mcp")
	user = str(frappe.session.user)
	return {
		"endpoint": endpoint,
		# The copy command can select clients with --client; the script uses --all by default.
		"install": install_command("--all"),
		"install_all": install_command("--all"),
		"install_hook": install_command("--all", "--hook", "--no-login"),
		"prompt": SETUP_PROMPT.format(endpoint=endpoint),
		"seen": last_seen(user),
		"oauth_off": [_(label) for field, label in OAUTH_FIELDS.items() if not _oauth(field)],
		"can_fix_oauth": frappe.has_permission("OAuth Settings", "write"),
		"reviews": frappe.get_list(
			"Code Review",
			filters={"source": "local", "requested_by": user},
			fields=["name", "repository", "branch", "findings_count", "confidence_score", "creation"],
			order_by="creation desc",
			limit=RECENT_REVIEWS,
		),
	}


def _oauth(field: str) -> bool:
	return bool(frappe.get_cached_value("OAuth Settings", "OAuth Settings", field))
