# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GuardTeam(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from guard.guard.doctype.guard_team_member.guard_team_member import GuardTeamMember

		avatar_url: DF.Data | None
		code_provider: DF.Link
		installation_id: DF.Data | None
		members: DF.Table[GuardTeamMember]
		namespace: DF.Data
		provider_namespace_id: DF.Data | None
		team_name: DF.Data
	# end: auto-generated types

	pass
