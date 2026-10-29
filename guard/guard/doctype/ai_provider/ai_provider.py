# Copyright (c) 2026, Sabu Siyad and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document

from guard import settings as guard_settings
from guard.llm.anthropic_dialect import AnthropicDialect
from guard.llm.openai_dialect import OpenAIDialect
from guard.llm.types import LLMError


class AIProvider(Document):
	# begin: auto-generated types
	# This code is auto-generated. Do not modify anything in this block.

	from typing import TYPE_CHECKING

	if TYPE_CHECKING:
		from frappe.types import DF

		api_key: DF.Password | None
		dialect: DF.Literal["openai", "anthropic"]
		enabled: DF.Check
		provider_name: DF.Data
		url: DF.Data
	# end: auto-generated types

	def validate(self):
		# Guard adds `/v1` itself. Vendors often show their base URL with it, so remove it.
		self.url = re.sub(r"/v1/?$", "", (self.url or "").strip()).rstrip("/")
		if not self.url.startswith(("https://", "http://")):
			frappe.throw(_("The base URL must start with https:// or http://."))
		if not self.enabled and not self.is_new():
			self.check_not_in_use()

	def check_not_in_use(self):
		"""Its models would run offline without a word. Guard Settings must pick another one first."""
		settings = guard_settings.settings()
		fields = ["default_ai_provider", *(f"{slot}_ai_provider" for slot in guard_settings.SLOTS)]
		if any(settings.get(field) == self.name for field in fields):
			frappe.throw(
				_("Guard Settings uses this AI provider. Pick another one there before you disable it.")
			)

	@frappe.whitelist()
	def fetch_models(self) -> list[str]:
		"""Model ids that the provider serves, from `/v1/models`."""
		self.validate()
		try:
			return self.models()
		except LLMError as error:
			frappe.throw(str(error), title=_("AI Provider"))
			return []

	def models(self) -> list[str]:
		# A key typed in the form and not saved yet. A saved key shows as asterisks.
		typed = self.api_key if self.api_key and set(self.api_key) != {"*"} else None
		key = str(typed or self.get_password("api_key", raise_exception=False) or "")
		timeout = guard_settings.request_timeout()
		# Each dialect has its own auth headers. The Anthropic front rejects a bearer token.
		dialect_class = AnthropicDialect if self.dialect == "anthropic" else OpenAIDialect
		return sorted(dialect_class(self.url, key, timeout).models())
