# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document

from guard.llm.anthropic_dialect import AnthropicDialect
from guard.llm.openai_dialect import OpenAIDialect
from guard.llm.types import LLMError


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
		cached_input_price: DF.Float
		chat_permission: DF.Literal["Everyone", "Organization members", "Guard users"]
		default_coding_agents: DF.SmallText | None
		embedding_dimensions: DF.Int
		embedding_model: DF.Data | None
		fast_model: DF.Data | None
		feature_tips: DF.Check
		gateway_api_key: DF.Password | None
		gateway_dialect: DF.Literal["openai", "anthropic"]
		gateway_url: DF.Data | None
		input_price: DF.Float
		learn_from_activity: DF.Check
		max_concurrent_reviews: DF.Int
		max_turns_apex: DF.Int
		max_turns_base: DF.Int
		max_turns_plus: DF.Int
		monthly_credit_cap: DF.Int
		output_price: DF.Float
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

	def validate(self):
		if self.monthly_credit_cap and self.monthly_credit_cap < 0:
			frappe.throw(_("The credit cap cannot be negative."))
		if self.embedding_dimensions and not 64 <= self.embedding_dimensions <= 8192:
			frappe.throw(_("Embedding dimensions must be between 64 and 8192."))

	def on_update(self):
		if self.has_value_changed("embedding_dimensions") or self.has_value_changed("embedding_model"):
			from guard.indexing.vectors import VectorStore

			frappe.clear_cache(doctype="Guard Settings")
			VectorStore.ensure_table()
			frappe.enqueue("guard.indexing.jobs.reembed_all", queue="long", enqueue_after_commit=True)

	@frappe.whitelist()
	def fetch_models(self) -> list[str]:
		"""Model ids that the gateway serves, from `/v1/models`."""
		if not self.gateway_url:
			frappe.throw(_("Set the gateway URL first."))
		url = (self.gateway_url or "").rstrip("/")
		key = str(self.get_password("gateway_api_key", raise_exception=False) or "")
		# Each dialect has its own auth headers. The Anthropic front rejects a bearer token.
		dialect_class = AnthropicDialect if self.gateway_dialect == "anthropic" else OpenAIDialect
		try:
			return sorted(dialect_class(url, key, self.request_timeout).models())
		except LLMError as error:
			frappe.throw(str(error), title=_("Gateway"))
			return []
