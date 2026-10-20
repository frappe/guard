# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CustomContext(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

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
		team: DF.Link | None
		title: DF.Data | None
		usage_count: DF.Int
	# end: auto-generated types

	pass
