# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class WebhookDelivery(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		action: DF.Data | None
		attempt: DF.Int
		code_provider: DF.Link | None
		delivery_id: DF.Data | None
		error: DF.LongText | None
		event: DF.Data | None
		payload: DF.Code | None
		repository_full_name: DF.Data | None
		status: DF.Literal["Received", "Processed", "Ignored", "Failed"]
	# end: auto-generated types

	pass
