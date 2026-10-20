# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GuardUser(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		bridge_seen_at: DF.Datetime | None
		coding_agents: DF.SmallText | None
		collapsible_sections: DF.Check
		github_login: DF.Data | None
		github_token: DF.Password | None
		show_diagram: DF.Check
		show_summary: DF.Check
		user: DF.Link
	# end: auto-generated types

	pass
