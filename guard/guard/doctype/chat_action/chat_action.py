# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ChatAction(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action_type: DF.Data | None
		chat_session: DF.Link
		draft: DF.Check
		executed_by: DF.Link | None
		payload: DF.JSON | None
		result: DF.LongText | None
		routine: DF.Check
		status: DF.Literal["Proposed", "Approved", "Executed", "Cancelled", "Failed"]
		summary: DF.SmallText | None
	# end: auto-generated types

	pass
