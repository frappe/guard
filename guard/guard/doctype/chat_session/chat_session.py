# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ChatSession(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from guard.guard.doctype.chat_message.chat_message import ChatMessage

		kind: DF.Literal["Pull Request", "Codebase", "API"]
		messages: DF.Table[ChatMessage]
		permission_mode: DF.Literal["Ask every time", "Allow routine actions", "Full access"]
		pull_request: DF.Link | None
		repositories: DF.JSON | None
		session_key: DF.Data | None
		title: DF.Data | None
		user: DF.Link | None
	# end: auto-generated types

	pass
