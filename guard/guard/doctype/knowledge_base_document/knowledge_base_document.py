# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class KnowledgeBaseDocument(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		area: DF.Data | None
		content: DF.MarkdownEditor | None
		edited_by_human: DF.Check
		generated_at: DF.Datetime | None
		path: DF.Data
		repository: DF.Link
		section: DF.Literal["index", "docs", "reverts"]
		source_files: DF.JSON | None
		stale: DF.Check
		title: DF.Data | None
		version_id: DF.Data | None
	# end: auto-generated types

	pass
