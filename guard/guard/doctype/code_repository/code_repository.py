# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from guard.permissions import is_guest


class CodeRepository(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		clone_url: DF.Data | None
		code_provider: DF.Link
		default_branch: DF.Data | None
		default_index: DF.Link | None
		full_name: DF.Data
		index_status: DF.Literal["Not Indexed", "Queued", "Indexing", "Indexed", "Failed"]
		is_private: DF.Check
		kb_status: DF.Literal["None", "Queued", "Building", "Ready", "Failed"]
		kb_version: DF.Data | None
		languages: DF.JSON | None
		provider_repo_id: DF.Data | None
		review_tier: DF.Literal["", "base", "plus", "apex", "auto"]
		reviews_enabled: DF.Check
		size_kb: DF.Int
		strictness: DF.Literal["", "1", "2", "3"]
		team: DF.Link | None
		web_url: DF.Data | None
	# end: auto-generated types

	def validate(self):
		# A share with write gives write before the has_permission hook runs. Guests only read.
		if is_guest() and not self.flags.ignore_permissions:
			frappe.throw(frappe._("A Guard Guest cannot change a repository."), frappe.PermissionError)

	def validate_share(self, share: Document) -> None:
		"""Frappe gives the rights of a share before the has_permission hook runs. A guest gets read
		only, and shares nothing: else they could share the repository with everyone."""
		if is_guest():
			frappe.throw(frappe._("A Guard Guest cannot share a repository."), frappe.PermissionError)
		rights = share.get("write") or share.get("share") or share.get("submit")
		if rights and not share.get("everyone") and is_guest(str(share.get("user") or "")):
			frappe.throw(frappe._("Share a repository with a Guard Guest with Can Read only."))
