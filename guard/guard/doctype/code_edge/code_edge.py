# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CodeEdge(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		index: DF.Link
		kind: DF.Literal["contains", "imports", "calls", "inherits", "references"]
		line: DF.Int
		path: DF.SmallText | None
		repository: DF.Link
		source_entity: DF.Data | None
		target_entity: DF.Data | None
		target_name: DF.Data | None
		target_text: DF.SmallText | None
	# end: auto-generated types

	pass
