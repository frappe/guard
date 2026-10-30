"""Delete the Desk page of the removed `guard` CLI. Migrate does not delete a Page whose folder is gone."""

import frappe


def execute():
	frappe.delete_doc("Page", "guard-cli-login", ignore_missing=True, force=True)
