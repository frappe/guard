"""Agent tokens: the credential that the setup prompt gives to a coding agent.

A token is an OAuth Bearer Token of the OAuth Client "Guard Agent", so Frappe checks it before Guard
runs, as for any OAuth sign-in. Unlike other OAuth tokens it works only on `/mcp` (`only_mcp`). The developer pastes one prompt, and the agent needs no browser
sign-in. Frappe keeps only the sha256 of the token, so Guard shows it once. The developer can revoke
it on the Connect page. It ends after 90 days, and Frappe drops it 30 days later.
"""

import secrets
from typing import Any

import frappe
from frappe.integrations.doctype.oauth_bearer_token.oauth_bearer_token import get_oauth_token_hash
from frappe.utils import add_to_date, now_datetime

from guard.settings import public_url
from guard.utils import cache, db

CLIENT = "Guard Agent"
SCOPES = "all"
# Seconds. The token is in the chat of the agent and in its config file, so it does not live long.
LIFETIME = 90 * 24 * 60 * 60
MAX_ACTIVE = 10  # for each user: a few computers and agents
MCP_PATHS = ("/mcp", "/mcp/")
CLIENT_LOCK_SECONDS = 30


def only_mcp() -> None:
	"""An auth hook. Frappe signs a bearer token in for the whole site. An agent token is for the MCP
	server only, so elsewhere the request goes on as Guest, and Frappe refuses it."""
	header = (frappe.get_request_header("Authorization") or "").split(" ")
	if len(header) != 2 or header[0].lower() != "bearer" or frappe.request.path in MCP_PATHS:
		return
	client = db.value("OAuth Bearer Token", {"access_token": get_oauth_token_hash(header[1])}, "client")
	if client and client == _client(create=False):
		frappe.set_user("Guest")  # nosemgrep: frappe-setuser -- drops rights, never adds them


def issue(user: str) -> str:
	"""A new token for `user`. The answer is the only copy of it."""
	if len(active(user)) >= MAX_ACTIVE:
		frappe.throw(frappe._("You have {0} agent tokens. Revoke one first.").format(MAX_ACTIVE))
	token = secrets.token_urlsafe(32)
	frappe.get_doc(
		{
			"doctype": "OAuth Bearer Token",
			"client": _client(create=True),
			"user": user,
			"scopes": SCOPES,
			"access_token": token,
			"expires_in": LIFETIME,
			"expiration_time": add_to_date(now_datetime(), seconds=LIFETIME),
			"status": "Active",
		}
	).insert(ignore_permissions=True)
	return token


def active(user: str) -> list[dict[str, Any]]:
	if not (client := _client(create=False)):
		return []
	return frappe.get_all(
		"OAuth Bearer Token",
		filters={
			"client": client,
			"user": user,
			"status": "Active",
			"expiration_time": [">", now_datetime()],
		},
		fields=["name", "creation", "expiration_time"],
		order_by="creation desc",
	)


def revoke(user: str, name: str) -> None:
	client = _client(create=False)
	if not client or db.value("OAuth Bearer Token", {"name": name, "client": client}, "user") != user:
		frappe.throw(frappe._("This is not one of your agent tokens."), frappe.PermissionError)
	db.set_values("OAuth Bearer Token", name, {"status": "Revoked"})


def _client(create: bool) -> str | None:
	"""The OAuth Client of the tokens. Nobody signs in with it: it only owns the tokens and their scope."""
	if (name := db.value("OAuth Client", {"app_name": CLIENT}, "name")) or not create:
		return str(name) if name else None
	with cache.exclusive("guard:agent-client", CLIENT_LOCK_SECONDS) as acquired:
		if not acquired:
			frappe.throw(frappe._("Guard is busy. Try again."))
		# Another request can have made it while this one waited for the lock.
		if name := db.value("OAuth Client", {"app_name": CLIENT}, "name"):
			return str(name)
		client = frappe.new_doc("OAuth Client")
		client.update(
			{
				"app_name": CLIENT,
				"software_id": "guard-agent",
				"scopes": SCOPES,
				"grant_type": "Authorization Code",
				"response_type": "Code",
				"redirect_uris": public_url("/desk/guard-agent"),
				"default_redirect_uri": public_url("/desk/guard-agent"),
			}
		)
		client.insert(ignore_permissions=True)
		# Commit now, so the next request finds it before the lock ends.
		db.commit()
		return str(client.name)
