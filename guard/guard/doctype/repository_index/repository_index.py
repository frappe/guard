# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class RepositoryIndex(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		branch: DF.Data
		chunk_count: DF.Int
		completed_at: DF.Datetime | None
		edge_count: DF.Int
		entity_count: DF.Int
		error: DF.LongText | None
		files_processed: DF.Int
		num_files: DF.Int
		parser_version: DF.Int
		repository: DF.Link
		sha: DF.Data | None
		started_at: DF.Datetime | None
		status: DF.Literal["submitted", "cloning", "processing", "completed", "failed"]
	# end: auto-generated types

	pass
