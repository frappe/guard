"""Merge the Pull Request rows that stand for the same pull request.

Events of a new pull request arrive in the same second, and each job could insert its own row. Keep
the oldest row, so that links to it stay valid. Give it the best values of all the rows, move the
links of the other rows to it, then delete them. Then add the unique index on (repository, number),
so that no new duplicates come.

Open findings of all the rows now belong to one pull request. The next review checks each of them
against its head and drops the repeats (`stale.close_stale`), as for any earlier review.
"""

from typing import Any, cast

import frappe
from frappe.model import no_value_fields

from guard.install import create_indexes
from guard.utils import db

# Fields that tell which pull request a row is. They are the same on all the rows of one group.
KEY_FIELDS = ("repository", "number")
# Fields read from GitHub. Each event writes all of them, so the row that changed last has the newest.
GITHUB_FIELDS = (
	"title",
	"description",
	"author",
	"state",
	"is_draft",
	"base_branch",
	"head_branch",
	"base_sha",
	"head_sha",
	"head_clone_url",
	"web_url",
	"provider_node_id",
	"labels",
	"opened_at",
	"merged_at",
	"closed_at",
	"additions",
	"deletions",
	"changed_files",
)
# Fields of the last review. They come together from the row of the newest completed review: a
# later event can change another row that has none of them.
REVIEW_FIELDS = (
	"last_reviewed_sha",
	"confidence_score",
	"risk_level",
	"summary_comment_id",
	"guard_approved",
	"approval_id",
	"tour",
	"tour_sha",
)
# Set from the Code Reviews after the merge.
COUNTED_FIELDS = ("latest_review", "review_count")


def execute():
	merge_all()
	try:
		create_indexes()
	except Exception as error:
		# The driver decides the class of the error. Frappe reads its code.
		if not frappe.db.is_duplicate_entry(cast(Any, error)):
			raise
		# A job inserted a duplicate after the merge. Merge it too.
		merge_all()
		create_indexes()


def merge_all() -> None:
	groups = db.rows(
		"""SELECT repository, number FROM `tabPull Request`
		GROUP BY repository, number HAVING COUNT(*) > 1"""
	)
	for repository, number in groups:
		names = frappe.get_all(
			"Pull Request",
			filters={"repository": repository, "number": number},
			order_by="creation asc, name asc",
			pluck="name",
		)
		merge(names[0], names[1:])


def merge(keep: str, duplicates: list[str]) -> None:
	"""Move the values and links of `duplicates` to `keep`, then delete them."""
	names = [keep, *duplicates]
	db.set_values("Pull Request", keep, _merged_values(names))
	for doctype, field in _links():
		frappe.db.set_value(doctype, {field: ["in", duplicates]}, field, keep, update_modified=False)
	completed = frappe.get_all(
		"Code Review",
		filters={"pull_request": keep, "status": "Completed"},
		order_by="completed_at desc, creation desc",
		pluck="name",
	)
	# Each review counted only the reviews of its own row. A running review sets both when it ends.
	db.set_values(
		"Pull Request",
		keep,
		{"latest_review": completed[0] if completed else None, "review_count": len(completed)},
	)
	for name in duplicates:
		# Not forced: a link that did not move stops the delete. A Deleted Document keeps a copy.
		frappe.delete_doc("Pull Request", name, ignore_permissions=True)


def _merged_values(names: list[str]) -> dict[str, Any]:
	rows = frappe.get_all(
		"Pull Request", filters={"name": ["in", names]}, fields=["*"], order_by="modified desc"
	)
	values = {field: rows[0][field] for field in GITHUB_FIELDS}
	owner = frappe.get_all(
		"Code Review",
		filters={"pull_request": ["in", names], "status": "Completed"},
		order_by="completed_at desc, creation desc",
		pluck="pull_request",
		limit=1,
	)
	if owner:
		row = next(row for row in rows if row.name == owner[0])
		values.update({field: row[field] for field in REVIEW_FIELDS})
	# Other fields, for example the state of the addressed check: the newest value that is set.
	keep = next(row for row in rows if row.name == names[0])
	for df in frappe.get_meta("Pull Request").fields:
		field = str(df.fieldname)
		if df.fieldtype in no_value_fields or field in values or field in KEY_FIELDS + COUNTED_FIELDS:
			continue
		values[field] = next((row[field] for row in rows if row[field]), keep[field])
	return values


def _links() -> list[tuple[str, str]]:
	"""(doctype, field) of each Link to Pull Request. Read from the meta, so a new one is not missed."""
	fields = frappe.get_all(
		"DocField",
		filters={"fieldtype": "Link", "options": "Pull Request", "parenttype": "DocType"},
		fields=["parent", "fieldname"],
	)
	fields += frappe.get_all(
		"Custom Field",
		filters={"fieldtype": "Link", "options": "Pull Request"},
		fields=["dt as parent", "fieldname"],
	)
	return [
		(str(field.parent), str(field.fieldname))
		for field in fields
		if not frappe.get_meta(str(field.parent)).issingle
	]
