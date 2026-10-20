# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class GuardSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		apex_model: DF.Data | None
		auto_enable_new_repositories: DF.Check
		base_model: DF.Data | None
		bot_logins: DF.SmallText | None
		chat_permission: DF.Literal["Everyone", "Organization members", "Guard users"]
		default_coding_agents: DF.SmallText | None
		embedding_dimensions: DF.Int
		embedding_model: DF.Data | None
		fast_model: DF.Data | None
		feature_tips: DF.Check
		gateway_api_key: DF.Password | None
		gateway_dialect: DF.Literal["openai", "anthropic"]
		gateway_url: DF.Data | None
		learn_from_activity: DF.Check
		max_turns_apex: DF.Int
		max_turns_base: DF.Int
		max_turns_plus: DF.Int
		monthly_credit_cap: DF.Int
		plus_model: DF.Data | None
		polling_enabled: DF.Check
		public_url: DF.Data | None
		request_timeout: DF.Int
		sandbox_enabled: DF.Check
		sandbox_image: DF.Data | None
		sandbox_network: DF.Check
		sandbox_runtime: DF.Literal["podman", "docker"]
		sandbox_setup_command: DF.SmallText | None
		sandbox_timeout: DF.Int
		sandbox_when: DF.Literal["On request", "Always", "Never"]
		teach_permission: DF.Literal["Everyone", "Organization members", "Guard users"]
		trigger_by_authoring: DF.Literal["Everyone", "Organization members", "Guard users"]
		trigger_by_tagging: DF.Literal["Everyone", "Organization members", "Guard users"]
		trigger_handles: DF.SmallText | None
	# end: auto-generated types

	pass
