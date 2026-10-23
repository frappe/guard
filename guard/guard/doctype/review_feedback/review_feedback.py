# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class ReviewFeedback(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		author: DF.Data | None
		body: DF.LongText | None
		finding: DF.Link | None
		kind: DF.Literal["Upvote", "Downvote", "Reply", "Addressed", "Ignored", "Missed"]
		processed: DF.Check
		provider_ref: DF.Data | None
		pull_request: DF.Link | None
		repository: DF.Link | None
		sentiment: DF.Literal["", "Agree", "Disagree", "Context", "Question", "Neutral"]
	# end: auto-generated types

	pass
