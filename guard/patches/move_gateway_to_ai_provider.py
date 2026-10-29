"""Keep the AI gateway of existing sites: it becomes the AI provider "Gateway", and the default.

Guard Settings no longer has the gateway fields, but the database still has their values: a
single DocType keeps them in `tabSingles`, and the key stays in `__Auth`.
"""

import frappe
from frappe.utils.password import get_decrypted_password

from guard.llm import breaker
from guard.utils import db

NAME = "Gateway"
OLD_FIELDS = ("gateway_url", "gateway_dialect", "gateway_api_key")


def execute():
	values = dict(
		db.rows(
			"SELECT field, value FROM `tabSingles` WHERE doctype = 'Guard Settings' AND field IN %s",
			(OLD_FIELDS,),
		)
	)
	url = str(values.get("gateway_url") or "").strip()
	# The old field took any text. An AI Provider needs a scheme, and the migrate must not stop.
	if url and not url.startswith(("https://", "http://")):
		url = f"https://{url}"
	if url and not frappe.db.exists("AI Provider", NAME):
		key = get_decrypted_password("Guard Settings", "Guard Settings", "gateway_api_key", False)
		frappe.get_doc(
			{
				"doctype": "AI Provider",
				"provider_name": NAME,
				"enabled": 1,
				"dialect": values.get("gateway_dialect") or "openai",
				"url": url,
				"api_key": key or None,
			}
		).insert(ignore_permissions=True)
		if not frappe.db.get_single_value("Guard Settings", "default_ai_provider"):
			frappe.db.set_single_value("Guard Settings", "default_ai_provider", NAME)
	frappe.db.delete("Singles", {"doctype": "Guard Settings", "field": ["in", OLD_FIELDS]})
	frappe.db.delete(
		"__Auth", {"doctype": "Guard Settings", "name": "Guard Settings", "fieldname": "gateway_api_key"}
	)
	frappe.clear_cache(doctype="Guard Settings")
	# The breaker of the gateway now has the name of its AI provider. A review that waits for the
	# old one would else wait for nothing.
	breaker.reset()
