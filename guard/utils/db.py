"""Typed helpers around `frappe.db.sql` for raw queries."""

from collections.abc import Sequence
from typing import Any, cast

import frappe

Values = tuple | list | dict | None


def rows(query: str, values: Values = None) -> list[tuple[Any, ...]]:
	return cast(list[tuple[Any, ...]], list(frappe.db.sql(query, values or ()) or ()))


def dict_rows(query: str, values: Values = None) -> list[dict[str, Any]]:
	return cast(list[dict[str, Any]], list(frappe.db.sql(query, values or (), as_dict=True) or ()))


def scalar(query: str, values: Values = None) -> Any:
	result = rows(query, values)
	return result[0][0] if result else None


def execute(query: str, values: Values = None) -> None:
	frappe.db.sql(query, values or ())


def insert_many(
	table: str,
	columns: Sequence[str],
	data: Sequence[Sequence[Any]],
	placeholders: Sequence[str] | None = None,
	replace: bool = False,
	batch: int = 200,
) -> None:
	"""Insert many rows with one statement for each batch.

	`placeholders` lets a column use an SQL function, for example `VEC_FromText(%s)`.
	"""
	if not data:
		return
	marks = placeholders or ["%s"] * len(columns)
	row_sql = "(" + ", ".join(marks) + ")"
	verb = "REPLACE" if replace else "INSERT"
	column_sql = ", ".join(f"`{column}`" for column in columns)
	for start in range(0, len(data), batch):
		chunk = data[start : start + batch]
		values = [value for row in chunk for value in row]
		# Table and column names come from code. Values are parameters.
		frappe.db.sql(  # nosemgrep: frappe-sql-format-injection
			f"{verb} INTO `{table}` ({column_sql}) VALUES {', '.join([row_sql] * len(chunk))}", values
		)


def single_int(doctype: str, field: str, default: int) -> int:
	value = frappe.db.get_single_value(doctype, field)
	try:
		return int(cast(Any, value)) if value not in (None, "") else default
	except TypeError, ValueError:
		return default


def value(doctype: str, name: str | dict, field: str) -> Any:
	return frappe.db.get_value(doctype, name, field)


def values(doctype: str, name: str | dict, fields: Sequence[str]) -> dict[str, Any] | None:
	"""Field values of one record as a dict, or None when it does not exist."""
	result = frappe.db.get_value(doctype, name, list(fields), as_dict=True)
	return cast(dict[str, Any], result) if result else None


def commit() -> None:
	"""Commit in background jobs. Tests run in one transaction, so skip there."""
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- background jobs commit their progress


def rollback() -> None:
	if not frappe.flags.in_test:
		frappe.db.rollback()


def set_values(doctype: str, name: str, values: dict[str, Any]) -> None:
	frappe.db.set_value(doctype, name, cast(Any, values))
