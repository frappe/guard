# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CodeEntity(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		content_hash: DF.Data | None
		docstring: DF.SmallText | None
		end_line: DF.Int
		entity_name: DF.Data | None
		index: DF.Link
		is_entry_point: DF.Check
		is_exported: DF.Check
		kind: DF.Literal[
			"file",
			"module",
			"class",
			"function",
			"method",
			"variable",
			"type",
			"interface",
			"doctype",
			"field",
		]
		language: DF.Data | None
		parent_entity: DF.Data | None
		path: DF.SmallText
		qualified_name: DF.SmallText | None
		repository: DF.Link
		signature: DF.SmallText | None
		start_line: DF.Int
	# end: auto-generated types

	pass
