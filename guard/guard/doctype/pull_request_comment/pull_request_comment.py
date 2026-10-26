# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PullRequestComment(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		attempt: DF.Int
		author: DF.Data | None
		body: DF.LongText | None
		commented_at: DF.Datetime | None
		eyes_reaction: DF.Data | None
		finding: DF.Link | None
		in_reply_to: DF.Data | None
		is_bot: DF.Check
		is_guard: DF.Check
		kind: DF.Literal["issue", "review", "inline"]
		line: DF.Int
		path: DF.SmallText | None
		processed: DF.Check
		provider_comment_id: DF.Data | None
		pull_request: DF.Link
		repository: DF.Link | None
		retry_at: DF.Datetime | None
		thread_id: DF.Data | None
		waits_for_review: DF.Check
	# end: auto-generated types

	pass
