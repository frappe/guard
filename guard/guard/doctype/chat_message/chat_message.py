# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ChatMessage(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		actions: DF.JSON | None
		content: DF.LongText | None
		context: DF.JSON | None
		parent: DF.Data
		parentfield: DF.Data
		parenttype: DF.Data
		role: DF.Literal["user", "assistant"]
		sources: DF.JSON | None
	# end: auto-generated types

	pass
