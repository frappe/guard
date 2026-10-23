# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import now_datetime


class CustomContext(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		active_since: DF.Datetime | None
		addressed_count: DF.Int
		addressed_rate: DF.Percent
		body: DF.LongText
		context_type: DF.Literal["Rule", "File", "Instruction", "Other", "Pattern"]
		evidence: DF.JSON | None
		file_path: DF.Data | None
		merged_count: DF.Int
		path_scope: DF.SmallText | None
		repository_scope: DF.SmallText | None
		rule_id: DF.Data | None
		severity: DF.Literal["", "low", "medium", "high"]
		source: DF.Literal["Dashboard", "Learned", "Suggested", "MCP", "Chat"]
		status: DF.Literal["Active", "Inactive", "Suggested", "Ignored"]
		status_reason: DF.SmallText | None
		team: DF.Link | None
		title: DF.Data | None
		usage_count: DF.Int
	# end: auto-generated types

	def validate(self):
		if not self.is_new() and self.has_value_changed("status"):
			self.track_status()

	def track_status(self) -> None:
		"""Say who changed the status. Activating starts a fresh window for automatic retirement."""
		if self.status == "Active":
			self.active_since = now_datetime()
			self.status_reason = ""
		elif not self.has_value_changed("status_reason"):
			self.status_reason = _("Set to {0} by {1}").format(_(self.status), frappe.session.user)

	def retire(self, reason: str) -> None:
		self.status = "Inactive"
		self.status_reason = reason
		self.save(ignore_permissions=True)
