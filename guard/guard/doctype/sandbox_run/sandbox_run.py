# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class SandboxRun(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		code_review: DF.Link
		command: DF.SmallText | None
		duration: DF.Float
		exit_code: DF.Int
		finding: DF.Link | None
		output: DF.LongText | None
		status: DF.Literal["Running", "Passed", "Failed", "Timed Out", "Error"]
	# end: auto-generated types

	pass
