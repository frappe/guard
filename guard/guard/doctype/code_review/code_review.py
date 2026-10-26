# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CodeReview(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		agent_turns: DF.Int
		attempt: DF.Int
		base_sha: DF.Data | None
		body: DF.LongText | None
		branch: DF.Data | None
		cached_tokens: DF.Int
		cancel_requested: DF.Check
		cancelled_by: DF.Link | None
		changed_files: DF.JSON | None
		check_run_id: DF.Data | None
		cli_payload: DF.JSON | None
		completed_at: DF.Datetime | None
		confidence_reason: DF.SmallText | None
		confidence_score: DF.Int
		config_snapshot: DF.JSON | None
		context_used: DF.JSON | None
		credits: DF.Int
		diagram: DF.Code | None
		diagram_type: DF.Literal["", "sequence", "er", "class", "flow"]
		duration: DF.Float
		error: DF.LongText | None
		eyes_reaction: DF.Data | None
		files_reviewed: DF.Int
		findings_count: DF.Int
		head_sha: DF.Data | None
		important_files: DF.JSON | None
		input_tokens: DF.Int
		instructions: DF.LongText | None
		job_id: DF.Data | None
		merge_base_sha: DF.Data | None
		output_tokens: DF.Int
		posted_count: DF.Int
		pull_request: DF.Link | None
		repository: DF.Link | None
		requested_by: DF.Data | None
		requested_tier: DF.Literal["", "base", "plus", "apex", "auto"]
		retry_at: DF.Datetime | None
		review_number: DF.Int
		risk_level: DF.Literal["", "low", "medium", "high", "critical"]
		risk_reason: DF.SmallText | None
		source: DF.Literal["pull_request", "cli", "api"]
		stage: DF.Data | None
		started_at: DF.Datetime | None
		status: DF.Literal["Queued", "Running", "Completed", "Failed", "Cancelled", "Skipped"]
		strictness: DF.Int
		summary: DF.MarkdownEditor | None
		suppressed_count: DF.Int
		tier: DF.Literal["", "base", "plus", "apex"]
		trigger: DF.Literal["open", "push", "rebase", "mention", "retrigger", "manual", "cli", "api"]
		triggered_by_user: DF.Link | None
	# end: auto-generated types

	pass
