# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

import json
from typing import cast

import frappe
from frappe import _
from frappe.model.document import Document

ORGANIZATION = "Organization"
FILTER_FIELDS = {
	"author",
	"target_branch",
	"source_branch",
	"label",
	"title",
	"keyword",
	"file_path",
	"files_changed",
	"repository",
}
FILTER_OPERATORS = {"include", "exclude", "more_than", "less_than"}


class ReviewSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from guard.guard.doctype.review_filter_rule.review_filter_rule import ReviewFilterRule

		auto_approve: DF.Check
		auto_approve_filters: DF.JSON | None
		auto_review: DF.Literal["Never", "On pull request opened", "On new pushes", "On all events"]
		comment_logic: DF.Check
		comment_style: DF.Check
		comment_syntax: DF.Check
		comments_outside_diff: DF.Check
		confidence_collapsible: DF.Check
		confidence_included: DF.Check
		confidence_open: DF.Check
		diagram_collapsible: DF.Check
		diagram_included: DF.Check
		diagram_open: DF.Check
		filter_rules: DF.Table[ReviewFilterRule]
		fix_with_ai: DF.Check
		hide_footer: DF.Check
		ignore_patterns: DF.Code | None
		include_rule_files: DF.Check
		inherit_from_organization: DF.Check
		instructions: DF.LongText | None
		impact_collapsible: DF.Check
		impact_included: DF.Check
		impact_open: DF.Check
		issues_table_collapsible: DF.Check
		issues_table_included: DF.Check
		issues_table_open: DF.Check
		review_drafts: DF.Check
		review_tier: DF.Literal["auto", "base", "plus", "apex"]
		risk_ceiling: DF.Literal["low", "medium", "high", "critical"]
		risk_definitions: DF.LongText | None
		scope: DF.Literal["Organization", "Team"]
		status_check: DF.Check
		status_comments: DF.Check
		strictness: DF.Literal["1", "2", "3"]
		summary_collapsible: DF.Check
		summary_included: DF.Check
		summary_open: DF.Check
		team: DF.Link | None
		update_description: DF.Check
		update_summary_only: DF.Check
	# end: auto-generated types

	def autoname(self):
		self.name = ORGANIZATION if self.scope == ORGANIZATION else self.team

	def validate(self):
		if self.scope == "Team" and not self.team:
			frappe.throw(_("A team record needs a team."))
		if self.scope == ORGANIZATION:
			self.team = None
			self.inherit_from_organization = 0
		for rule in self.filter_rules:
			validate_conditions(rule.conditions)

	@frappe.whitelist()
	def sync_from_organization(self) -> None:
		"""Copy every review field from the organization record."""
		if self.scope != "Team":
			return
		source = cast(ReviewSettings, frappe.get_doc("Review Settings", ORGANIZATION))
		skip = {"name", "scope", "team", "inherit_from_organization", "owner", "creation", "modified"}
		for field in self.meta.get_valid_columns():
			if field not in skip:
				self.set(field, source.get(field))
		self.set("filter_rules", [])
		for row in source.filter_rules:
			self.append("filter_rules", row.as_dict(no_default_fields=True))
		self.save()


def validate_conditions(raw: str | list | None) -> None:
	try:
		conditions = json.loads(raw) if isinstance(raw, str) else (raw or [])
	except json.JSONDecodeError:
		frappe.throw(_("Filter conditions must be valid JSON."))
		return
	if not isinstance(conditions, list):
		frappe.throw(_("Filter conditions must be a list."))
	for condition in conditions:
		if condition.get("field") not in FILTER_FIELDS:
			frappe.throw(_("Unknown filter field: {0}").format(condition.get("field")))
		if condition.get("operator") not in FILTER_OPERATORS:
			frappe.throw(_("Unknown filter operator: {0}").format(condition.get("operator")))


def get_effective(team: str | None) -> Document:
	"""Review Settings for a team: its own record unless it inherits."""
	if team and frappe.db.exists("Review Settings", team):
		doc = frappe.get_cached_doc("Review Settings", team)
		if not doc.get("inherit_from_organization"):
			return doc
	return frappe.get_cached_doc("Review Settings", ORGANIZATION)
