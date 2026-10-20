# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GuardIntegration(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_token: DF.Password | None
		email: DF.Data | None
		enabled: DF.Check
		instructions: DF.LongText | None
		integration_type: DF.Literal["Jira", "Linear"]
		last_tested_at: DF.Datetime | None
		linear_teams: DF.SmallText | None
		site_url: DF.Data | None
	# end: auto-generated types

	pass
