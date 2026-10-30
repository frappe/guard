# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

from typing import TYPE_CHECKING, cast

import frappe
from frappe import _
from frappe.model.document import Document

from guard.llm.types import LLMError
from guard.settings import SLOTS

if TYPE_CHECKING:
	from guard.guard.doctype.ai_provider.ai_provider import AIProvider


class GuardSettings(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		apex_ai_provider: DF.Link | None
		apex_cached_input_price: DF.Float
		apex_input_price: DF.Float
		apex_model: DF.Data | None
		apex_output_price: DF.Float
		auto_enable_new_repositories: DF.Check
		base_ai_provider: DF.Link | None
		base_cached_input_price: DF.Float
		base_input_price: DF.Float
		base_model: DF.Data | None
		base_output_price: DF.Float
		bot_logins: DF.SmallText | None
		chat_permission: DF.Literal["Everyone", "Organization members", "Guard users"]
		default_ai_provider: DF.Link | None
		default_coding_agents: DF.SmallText | None
		embedding_ai_provider: DF.Link | None
		embedding_dimensions: DF.Int
		embedding_model: DF.Data | None
		fast_ai_provider: DF.Link | None
		fast_model: DF.Data | None
		feature_tips: DF.Check
		learn_from_activity: DF.Check
		max_concurrent_reviews: DF.Int
		max_turns_apex: DF.Int
		max_turns_base: DF.Int
		max_turns_plus: DF.Int
		monthly_credit_cap: DF.Int
		offline_mode: DF.Check
		outage_fallback_after: DF.Int
		plus_ai_provider: DF.Link | None
		plus_cached_input_price: DF.Float
		plus_input_price: DF.Float
		plus_model: DF.Data | None
		plus_output_price: DF.Float
		polling_enabled: DF.Check
		public_url: DF.Data | None
		pull_request_daily_tokens: DF.Int
		push_review_delay: DF.Int
		request_timeout: DF.Int
		reviews_per_worker: DF.Int
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
		self.validate_ai_providers()

	def validate_ai_providers(self):
		"""A model on a disabled AI provider would run offline without a word: no findings, or hash
		embeddings mixed with model vectors."""
		disabled = set(frappe.get_all("AI Provider", filters={"enabled": 0}, pluck="name"))
		for slot in SLOTS:
			name = self.get(f"{slot}_ai_provider") or self.default_ai_provider
			if name in disabled and self.get(f"{slot}_model"):
				frappe.throw(
					_("The {0} model uses the AI provider {1}, which is disabled.").format(slot, name)
				)

	def on_update(self):
		before = self.get_doc_before_save()
		embeddings = (self.embedding_dimensions, *_embedding_route(self))
		if before is None or embeddings != (before.embedding_dimensions, *_embedding_route(before)):
			from guard.indexing.vectors import VectorStore

			frappe.clear_cache(doctype="Guard Settings")
			VectorStore.ensure_table()
			frappe.enqueue("guard.indexing.jobs.reembed_all", queue="long", enqueue_after_commit=True)

	@frappe.whitelist()
	def fetch_models(self) -> dict[str, list[str]]:
		"""Model ids for each model field, from `/v1/models` of the AI provider of the field."""
		names = {
			slot: str(self.get(f"{slot}_ai_provider") or self.default_ai_provider or "") for slot in SLOTS
		}
		if not any(names.values()):
			frappe.throw(_("Set the default AI provider first."))
		models: dict[str, list[str]] = {}
		for name in {name for name in names.values() if name}:
			provider = cast("AIProvider", frappe.get_doc("AI Provider", name))
			try:
				models[name] = provider.models()
			except LLMError as error:
				frappe.throw(f"{name}: {error}", title=_("AI Provider"))
		return {slot: models.get(name or "", []) for slot, name in names.items()}


def _embedding_route(doc: Document) -> tuple[str, str]:
	"""The embedding model and its AI provider. A change of either makes all vectors stale. Without a
	model, or in offline mode, Guard uses hash embeddings, whatever the AI provider."""
	model = str(doc.get("embedding_model") or "")
	if not model or doc.get("offline_mode"):
		return "", ""
	return model, str(doc.get("embedding_ai_provider") or doc.get("default_ai_provider") or "")
