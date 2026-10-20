# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CodeRepository(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		clone_url: DF.Data | None
		code_provider: DF.Link
		default_branch: DF.Data | None
		default_index: DF.Link | None
		full_name: DF.Data
		index_status: DF.Literal["Not Indexed", "Queued", "Indexing", "Indexed", "Failed"]
		is_private: DF.Check
		kb_status: DF.Literal["None", "Building", "Ready", "Failed"]
		kb_version: DF.Data | None
		languages: DF.JSON | None
		provider_repo_id: DF.Data | None
		review_tier: DF.Literal["", "base", "plus", "apex", "auto"]
		reviews_enabled: DF.Check
		size_kb: DF.Int
		strictness: DF.Literal["", "1", "2", "3"]
		team: DF.Link | None
		web_url: DF.Data | None
	# end: auto-generated types

	pass
