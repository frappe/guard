"""The webhook URL that GitHub calls."""

import json
from typing import Any

import frappe
from frappe import _

from guard.utils import db
from guard.webhooks import github

MAX_PAYLOAD = 2 * 1024 * 1024


@frappe.whitelist(allow_guest=True, methods=["POST"])
def github_webhook(**kwargs: Any) -> dict[str, Any]:
	"""Check the signature, store the delivery, queue the handler."""
	body = frappe.request.get_data() or b""
	headers = {key: value for key, value in frappe.request.headers.items()}
	provider = _provider(frappe.request.args.get("provider"))
	secret = frappe.get_doc("Code Provider", provider).get_password("webhook_secret", raise_exception=False)
	if not secret or not github.verify(headers, body, str(secret)):
		frappe.local.response.http_status_code = 401
		return {"ok": False, "error": "invalid signature"}
	delivery = github.delivery_id(headers) or frappe.generate_hash()
	if frappe.db.exists("Webhook Delivery", {"delivery_id": delivery}):
		return {"ok": True, "duplicate": True}
	event = github.event_name(headers)
	payload = _payload(body)
	doc = frappe.get_doc(
		{
			"doctype": "Webhook Delivery",
			"code_provider": provider,
			"delivery_id": delivery,
			"event": event,
			"action": str(payload.get("action") or ""),
			"repository_full_name": (payload.get("repository") or {}).get("full_name") or "",
			"payload": body[:MAX_PAYLOAD].decode("utf-8", "replace"),
		}
	).insert(ignore_permissions=True)
	if event == "ping":
		doc.db_set("status", "Processed")
		return {"ok": True, "pong": True}
	frappe.enqueue(
		"guard.webhooks.handler.process",
		queue="short",
		enqueue_after_commit=True,
		delivery=doc.name,
	)
	return {"ok": True}


def _provider(name: str | None) -> str:
	if name and frappe.db.exists("Code Provider", name):
		return name
	enabled = frappe.get_all("Code Provider", filters={"enabled": 1}, pluck="name")
	if len(enabled) == 1:
		return enabled[0]
	frappe.throw(_("Add ?provider=<Code Provider> to the webhook URL."), frappe.ValidationError)
	return ""


def _payload(body: bytes) -> dict[str, Any]:
	try:
		data = json.loads(body or b"{}")
	except json.JSONDecodeError:
		return {}
	return data if isinstance(data, dict) else {}


def webhook_url(provider: str) -> str:
	from guard.settings import public_url

	return public_url(f"/api/method/guard.webhooks.endpoint.github_webhook?provider={provider}")


def cleanup_deliveries() -> None:
	"""Delete deliveries older than 30 days."""
	db.execute("DELETE FROM `tabWebhook Delivery` WHERE creation < NOW() - INTERVAL 30 DAY")
