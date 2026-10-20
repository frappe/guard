# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CodeChunk(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		content: DF.LongText | None
		content_hash: DF.Data | None
		end_line: DF.Int
		entity: DF.Data | None
		index: DF.Link
		language: DF.Data | None
		path: DF.SmallText
		repository: DF.Link
		start_line: DF.Int
	# end: auto-generated types

	pass
