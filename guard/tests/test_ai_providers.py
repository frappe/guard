from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import frappe
import httpx
from frappe.tests import IntegrationTestCase, UnitTestCase

from guard import settings as guard_settings
from guard.llm import breaker
from guard.llm.client import ChatRequest, LLMClient, use_responder
from guard.llm.embeddings import Embedder
from guard.llm.types import ChatResult, GatewayBusy, GatewayDown, Message
from guard.settings import AIProviderConfig, GatewayConfig

if TYPE_CHECKING:
	from guard.guard.doctype.guard_settings.guard_settings import GuardSettings

OPENAI_ANSWER = {
	"choices": [{"message": {"content": "from openai"}, "finish_reason": "stop"}],
	"usage": {"prompt_tokens": 3, "completion_tokens": 1},
}
ANTHROPIC_ANSWER = {
	"content": [{"type": "text", "text": "from anthropic"}],
	"usage": {},
	"stop_reason": "end_turn",
}
# Every field of Guard Settings that these tests change. Other tests expect them blank: offline.
FIELDS = (
	"default_ai_provider",
	*(f"{slot}_ai_provider" for slot in guard_settings.SLOTS),
	"base_model",
	"plus_model",
	"apex_model",
	"fast_model",
	"embedding_model",
)


class TestRoutes(UnitTestCase):
	def test_a_tier_without_a_model_uses_the_lower_tier_and_its_ai_provider(self):
		a, b = AIProviderConfig("a", "https://a"), AIProviderConfig("b", "https://b")
		config = GatewayConfig(
			models={"base": "m-base", "plus": "", "apex": "m-apex"},
			ai_providers={"a": a, "b": b},
			default_ai_provider="a",
			slot_ai_providers={"base": "b", "plus": "a"},
		)
		self.assertEqual(config.route("plus"), config.route("base"))
		self.assertEqual((config.route("base").model, config.route("base").ai_provider), ("m-base", b))
		self.assertEqual((config.route("apex").model, config.route("apex").ai_provider), ("m-apex", a))
		# No fast model: the fast slot is the base tier, with its AI provider.
		self.assertEqual(config.route("fast"), config.route("base"))
		config.fast_model = "m-fast"
		self.assertEqual((config.route("fast").model, config.route("fast").ai_provider), ("m-fast", a))

	def test_an_unknown_or_disabled_ai_provider_is_offline(self):
		config = GatewayConfig(models={"base": "m"}, default_ai_provider="gone")
		self.assertFalse(config.route("base").online)
		self.assertEqual(config.route("base").ai_provider_name, "")


class TestAIProviders(IntegrationTestCase):
	def setUp(self):
		for field in FIELDS:
			self.addCleanup(self.set, field, None)
		self.addCleanup(breaker.reset)
		self.openrouter = self.provider("OpenRouter", "https://openrouter.ai/api/v1/", "openai", "or-key")
		self.anthropic = self.provider("Anthropic", "https://api.anthropic.com", "anthropic", "an-key")

	def provider(self, name: str, url: str, dialect: str, key: str) -> str:
		if frappe.db.exists("AI Provider", name):
			frappe.delete_doc("AI Provider", name, force=True)
		doc = frappe.get_doc(
			{"doctype": "AI Provider", "provider_name": name, "url": url, "dialect": dialect, "api_key": key}
		).insert(ignore_permissions=True)
		return str(doc.name)

	def set(self, field: str, value: Any) -> None:
		frappe.db.set_single_value("Guard Settings", field, value)
		frappe.clear_document_cache("Guard Settings", "Guard Settings")

	def configure(self, **values: Any) -> None:
		for field, value in values.items():
			self.set(field, value)

	def test_the_base_url_loses_v1_and_the_trailing_slash(self):
		self.assertEqual(
			frappe.db.get_value("AI Provider", self.openrouter, "url"), "https://openrouter.ai/api"
		)
		with self.assertRaises(frappe.ValidationError):
			self.provider("Bad", "openrouter.ai/api", "openai", "")

	def test_each_model_calls_its_own_ai_provider(self):
		self.configure(
			default_ai_provider=self.openrouter,
			apex_ai_provider=self.anthropic,
			base_model="deepseek/deepseek-v4",
			apex_model="claude-opus-5-5",
		)
		calls: list[tuple[str, dict[str, str], str]] = []

		def post(url: str, json: dict, headers: dict, timeout: float) -> httpx.Response:
			calls.append((url, headers, json["model"]))
			answer = ANTHROPIC_ANSWER if url.endswith("/v1/messages") else OPENAI_ANSWER
			return httpx.Response(200, json=answer)

		question = [Message("user", "hi")]
		with patch("guard.llm.http.httpx.post", side_effect=post):
			self.assertEqual(LLMClient("base").chat(question).content, "from openai")
			self.assertEqual(LLMClient("apex").chat(question).content, "from anthropic")
			# No plus model: plus uses the base model on the AI provider of base.
			self.assertEqual(LLMClient("plus").chat(question).content, "from openai")
			LLMClient.fast().chat(question)
		self.assertEqual(
			[(url, model) for url, _, model in calls],
			[
				("https://openrouter.ai/api/v1/chat/completions", "deepseek/deepseek-v4"),
				("https://api.anthropic.com/v1/messages", "claude-opus-5-5"),
				("https://openrouter.ai/api/v1/chat/completions", "deepseek/deepseek-v4"),
				("https://openrouter.ai/api/v1/chat/completions", "deepseek/deepseek-v4"),
			],
		)
		self.assertEqual(calls[0][1], {"Authorization": "Bearer or-key"})
		self.assertEqual(calls[1][1]["x-api-key"], "an-key")

	def test_a_disabled_ai_provider_leaves_its_models_offline(self):
		self.configure(default_ai_provider=self.openrouter, base_model="m")
		frappe.db.set_value("AI Provider", self.openrouter, "enabled", 0)
		self.assertFalse(LLMClient("base").online)

	def test_an_error_names_its_ai_provider(self):
		self.configure(default_ai_provider=self.openrouter, base_model="m")

		def down(request: ChatRequest) -> ChatResult:
			raise GatewayDown("Gateway answered 502")

		with use_responder(down), self.assertRaises(GatewayDown) as raised:
			LLMClient("base").chat([Message("user", "hi")])
		self.assertEqual(raised.exception.ai_provider, self.openrouter)

	def test_embeddings_use_the_embedding_ai_provider(self):
		self.configure(
			default_ai_provider=self.anthropic,
			embedding_ai_provider=self.openrouter,
			embedding_model="embed-1",
		)
		urls: list[str] = []

		def post(url: str, json: dict, headers: dict, timeout: float) -> httpx.Response:
			urls.append(url)
			return httpx.Response(200, json={"data": [{"index": 0, "embedding": [0.5, 0.5]}]})

		with patch("guard.llm.http.httpx.post", side_effect=post):
			self.assertEqual(Embedder().embed(["a"]), [[0.5, 0.5]])
		self.assertEqual(urls, ["https://openrouter.ai/api/v1/embeddings"])

	def test_fetch_models_asks_the_ai_provider_of_each_field(self):
		self.configure(default_ai_provider=self.openrouter, apex_ai_provider=self.anthropic)

		def get(url: str, headers: dict, timeout: float) -> httpx.Response:
			model = "claude" if "anthropic" in url else "deepseek"
			return httpx.Response(200, json={"data": [{"id": model}]})

		with patch("guard.llm.http.httpx.get", side_effect=get):
			models = cast("GuardSettings", frappe.get_single("Guard Settings")).fetch_models()
		self.assertEqual(models["apex"], ["claude"])
		self.assertEqual(models["base"], ["deepseek"])
		self.assertEqual(models["embedding"], ["deepseek"])

	def test_an_ai_provider_in_use_cannot_be_disabled_or_point_a_model_offline(self):
		self.configure(apex_ai_provider=self.anthropic, apex_model="claude-opus-5-5")
		doc = frappe.get_doc("AI Provider", self.anthropic)
		doc.set("enabled", 0)
		with self.assertRaises(frappe.ValidationError):
			doc.save(ignore_permissions=True)
		frappe.db.set_value("AI Provider", self.openrouter, "enabled", 0)
		settings = frappe.get_single("Guard Settings")
		settings.update({"plus_ai_provider": self.openrouter, "plus_model": "m"})
		with self.assertRaises(frappe.ValidationError):
			settings.save(ignore_permissions=True)

	def test_an_embedding_error_names_its_ai_provider(self):
		self.configure(default_ai_provider=self.openrouter, embedding_model="embed-1")
		with (
			patch("guard.llm.http.httpx.post", return_value=httpx.Response(402, json={})),
			self.assertRaises(GatewayBusy) as raised,
		):
			Embedder().embed(["a"])
		self.assertEqual(raised.exception.ai_provider, self.openrouter)

	def test_a_recovery_reruns_only_the_reviews_of_its_ai_provider(self):
		from frappe.utils import add_to_date, now_datetime

		since = str(add_to_date(now_datetime(), minutes=-1))
		failed = {self.openrouter: "", self.anthropic: ""}
		for name in failed:
			failed[name] = str(
				frappe.get_doc(
					{
						"doctype": "Code Review",
						"source": "pull_request",
						"status": "Failed",
						"error": f"{breaker.FAILED_PREFIX} ({name}): Gateway answered 502",
						"completed_at": now_datetime(),
					}
				)
				.insert(ignore_permissions=True, ignore_mandatory=True)
				.name
			)
		with patch("guard.llm.breaker.retry_review") as retry:
			self.assertEqual(breaker.rerun_failed(since, self.openrouter), [failed[self.openrouter]])
		retry.assert_called_once_with(failed[self.openrouter])

	def test_an_outage_pauses_only_its_own_ai_provider(self):
		with patch("guard.llm.breaker._notify_admins") as notify:
			for _ in range(breaker.THRESHOLD):
				breaker.record_failure("Gateway answered 502", self.openrouter)
		notify.assert_called_once()
		self.assertGreater(breaker.wait_seconds([self.openrouter]), 0)
		self.assertEqual(breaker.wait_seconds([self.anthropic, ""]), 0)
		self.assertGreater(breaker.wait_seconds(), 0, "all AI providers")
		self.assertEqual(breaker.status(self.anthropic)["paused_for"], 0)
		breaker.record_success(self.openrouter)
		self.assertEqual(breaker.wait_seconds(), 0)
