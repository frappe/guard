# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class PullRequest(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		additions: DF.Int
		addressed_attempt: DF.Int
		addressed_retry_at: DF.Datetime | None
		addressed_since: DF.Data | None
		approval_id: DF.Data | None
		author: DF.Data | None
		base_branch: DF.Data | None
		base_sha: DF.Data | None
		changed_files: DF.Int
		closed_at: DF.Datetime | None
		confidence_score: DF.Int
		deletions: DF.Int
		description: DF.LongText | None
		first_commit_sha: DF.Data | None
		guard_approved: DF.Check
		head_branch: DF.Data | None
		head_clone_url: DF.Data | None
		head_sha: DF.Data | None
		is_draft: DF.Check
		labels: DF.JSON | None
		last_reviewed_sha: DF.Data | None
		latest_review: DF.Link | None
		merged_at: DF.Datetime | None
		number: DF.Int
		opened_at: DF.Datetime | None
		provider_node_id: DF.Data | None
		repository: DF.Link
		retriggered_at: DF.Datetime | None
		review_count: DF.Int
		risk_level: DF.Literal["", "low", "medium", "high", "critical"]
		state: DF.Literal["open", "closed", "merged"]
		summary_comment_id: DF.Data | None
		title: DF.SmallText
		tour: DF.JSON | None
		tour_sha: DF.Data | None
		web_url: DF.Data | None
	# end: auto-generated types

	pass
