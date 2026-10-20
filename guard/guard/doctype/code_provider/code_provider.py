# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class CodeProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_url: DF.Data | None
		app_id: DF.Data | None
		app_slug: DF.Data | None
		auth_mode: DF.Literal["Token", "App"]
		bot_username: DF.Data | None
		enabled: DF.Check
		private_key: DF.Password | None
		provider_type: DF.Literal["GitHub", "GitLab", "Gitea", "Bitbucket"]
		title: DF.Data
		token: DF.Password | None
		verify_ssl: DF.Check
		web_url: DF.Data | None
		webhook_secret: DF.Password | None
		webhook_url: DF.Data | None
	# end: auto-generated types

	pass
