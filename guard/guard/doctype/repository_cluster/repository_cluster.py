# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class RepositoryCluster(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		from guard.guard.doctype.repository_cluster_member.repository_cluster_member import (
			RepositoryClusterMember,
		)

		cluster_name: DF.Data
		confidence: DF.Float
		members: DF.Table[RepositoryClusterMember]
		status: DF.Literal["Active", "Suggested", "Discarded"]
	# end: auto-generated types

	pass
