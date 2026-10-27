"""Typed helpers around `frappe.db.sql` for raw queries."""

from collections.abc import Callable, Sequence
from typing import Any, cast

import frappe
from frappe.utils import now_datetime

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


def values(
	doctype: str, name: str | dict, fields: Sequence[str], for_update: bool = False
) -> dict[str, Any] | None:
	"""Field values of one record as a dict, or None when it does not exist.

	`for_update` locks the row until the transaction ends, so that a change based on these values
	cannot race with another one.
	"""
	result = frappe.db.get_value(doctype, name, list(fields), as_dict=True, for_update=for_update)
	return cast(dict[str, Any], result) if result else None


def due(field: str) -> list[list[Any]]:
	"""Filters for rows whose `field` time is set and has passed.

	Frappe compiles `[field, "<=", now]` to `IFNULL(field, '') <= now`, which matches every NULL row.
	"""
	return [[field, "is", "set"], [field, "<=", now_datetime()]]


def value_for_update(doctype: str, name: str, field: str) -> Any:
	"""One field of a record, with the row locked until the transaction ends."""
	return frappe.db.get_value(doctype, name, field, for_update=True)


def commit() -> None:
	"""Commit in background jobs. Tests run in one transaction, so skip there."""
	if not frappe.flags.in_test:
		frappe.db.commit()  # nosemgrep: frappe-manual-commit -- background jobs commit their progress


def after_commit(action: Callable[[], None]) -> None:
	"""Run `action` when the transaction commits, so it acts only on state that is saved. Its own
	writes are committed after it: the request has no later commit.

	Tests run in one transaction that never commits, so there it runs at once.
	"""
	if frappe.flags.in_test:
		action()
		return

	def run() -> None:
		action()
		commit()

	frappe.db.after_commit.add(run)


def rollback() -> None:
	if not frappe.flags.in_test:
		frappe.db.rollback()


def close() -> None:
	"""Close the connection of a job that waits for a long time. The next query connects again. Tests
	keep it: they run in one transaction."""
	if not frappe.flags.in_test:
		frappe.db.close()


def set_values(doctype: str, name: str, values: dict[str, Any]) -> None:
	frappe.db.set_value(doctype, name, cast(Any, values))
