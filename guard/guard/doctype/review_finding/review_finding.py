# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ReviewFinding(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		addressed_at: DF.Datetime | None
		addressed_in_sha: DF.Data | None
		author: DF.Data | None
		body: DF.MarkdownEditor | None
		category: DF.Data | None
		code_review: DF.Link
		comment_type: DF.Literal["logic", "syntax", "style"]
		comment_url: DF.Data | None
		confidence: DF.Float
		custom_context: DF.Link | None
		downvotes: DF.Int
		end_line: DF.Int
		fingerprint: DF.Data | None
		fix_prompt: DF.Code | None
		is_security: DF.Check
		outside_diff: DF.Check
		path: DF.SmallText | None
		posted: DF.Check
		provider_comment_id: DF.Data | None
		provider_thread_id: DF.Data | None
		pull_request: DF.Link | None
		question: DF.SmallText | None
		repository: DF.Link | None
		sandbox_log: DF.LongText | None
		severity: DF.Literal["P0", "P1", "P2"]
		side: DF.Literal["RIGHT", "LEFT"]
		start_line: DF.Int
		status: DF.Literal["Open", "Addressed", "Ignored", "Dismissed", "Suppressed", "Outdated"]
		status_reason: DF.SmallText | None
		suggestion: DF.Code | None
		title: DF.SmallText | None
		upvotes: DF.Int
		verified: DF.Check
	# end: auto-generated types

	pass
