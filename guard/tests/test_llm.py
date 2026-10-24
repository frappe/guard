import threading
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from guard.llm.anthropic_dialect import _messages
from guard.llm.anthropic_dialect import _result as anthropic_result
from guard.llm.client import PARALLEL_CALLS, ChatRequest, LLMClient, parse_json, use_responder
from guard.llm.embeddings import hash_embedding, tokens
from guard.llm.offline import OfflineDialect
from guard.llm.openai_dialect import _result as openai_result
from guard.llm.types import ChatResult, LLMError, Message, ToolCall, ToolSpec, Usage

SUBMIT = ToolSpec(
	"submit_review",
	"End the review",
	{
		"type": "object",
		"properties": {
			"summary": {"type": "string"},
			"confidence_score": {"type": "integer"},
			"risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
		},
		"required": ["summary", "confidence_score", "risk_level"],
	},
)


def cosine(a, b):
	return sum(x * y for x, y in zip(a, b, strict=True))


class TestParseJson(UnitTestCase):
	def test_fenced(self):
		self.assertEqual(parse_json('Here:\n```json\n{"a": 1}\n```'), {"a": 1})

	def test_trailing_comma(self):
		self.assertEqual(parse_json('{"a": [1, 2,],}'), {"a": [1, 2]})

	def test_array(self):
		self.assertEqual(parse_json("result: [1, 2]"), [1, 2])

	def test_none(self):
		self.assertIsNone(parse_json("no json here"))


class TestEmbeddings(UnitTestCase):
	def test_tokens_split_identifiers(self):
		self.assertEqual(tokens("getUserById snake_case"), ["get", "user", "by", "id", "snake", "case"])

	def test_hash_embedding_similarity(self):
		a = hash_embedding("def update_user_email(user, email): validate(email)", 256)
		b = hash_embedding("def update_user_profile(user, data): validate(data)", 256)
		c = hash_embedding("SELECT price FROM invoice ORDER BY date", 256)
		self.assertGreater(cosine(a, b), cosine(a, c))
		self.assertAlmostEqual(cosine(a, a), 1.0, places=5)


class TestOffline(UnitTestCase):
	def test_forced_tool_gets_defaults(self):
		result = OfflineDialect().chat("m", [Message("user", "hi")], [SUBMIT], "submit_review", 100, 0)
		self.assertEqual(result.tool_calls[0].name, "submit_review")
		self.assertEqual(result.tool_calls[0].arguments["confidence_score"], 0)
		self.assertEqual(result.tool_calls[0].arguments["risk_level"], "critical")

	def test_unforced_returns_nothing(self):
		result = OfflineDialect().chat("m", [Message("user", "hi")], [SUBMIT], None, 100, 0)
		self.assertEqual(result.tool_calls, [])


class TestDialects(UnitTestCase):
	def test_anthropic_merges_tool_results(self):
		messages = [
			Message("user", "review"),
			Message("assistant", "", [ToolCall("t1", "read_file", {}), ToolCall("t2", "grep", {})]),
			Message("tool", "file", tool_call_id="t1"),
			Message("tool", "hits", tool_call_id="t2"),
		]
		converted = _messages(messages)
		self.assertEqual(len(converted), 3)
		self.assertEqual([b["tool_use_id"] for b in converted[2]["content"]], ["t1", "t2"])

	def test_anthropic_cache_control(self):
		converted = _messages([Message("user", "big context", cache=True)])
		self.assertEqual(converted[0]["content"][0]["cache_control"], {"type": "ephemeral"})
		results = _messages(
			[Message("tool", "a", tool_call_id="t1"), Message("tool", "b", tool_call_id="t2", cache=True)]
		)
		self.assertEqual(["cache_control" in block for block in results[0]["content"]], [False, True])

	def test_anthropic_result(self):
		result = anthropic_result(
			{
				"content": [
					{"type": "text", "text": "ok"},
					{"type": "tool_use", "id": "x", "name": "grep", "input": {"pattern": "a"}},
				],
				"usage": {
					"input_tokens": 10,
					"output_tokens": 5,
					"cache_read_input_tokens": 7,
					"cache_creation_input_tokens": 3,
				},
			},
			"m",
		)
		self.assertEqual(result.content, "ok")
		self.assertEqual(result.tool_calls[0].arguments, {"pattern": "a"})
		self.assertEqual(result.usage.cached_tokens, 7)
		self.assertEqual(result.usage.input_tokens, 20, "the whole prompt, as on the OpenAI front")

	def test_openai_result(self):
		result = openai_result(
			{
				"choices": [
					{
						"message": {
							"content": None,
							"tool_calls": [
								{"id": "c", "function": {"name": "grep", "arguments": '{"pattern": "x"}'}}
							],
						},
						"finish_reason": "tool_calls",
					}
				],
				"usage": {
					"prompt_tokens": 3,
					"completion_tokens": 4,
					"prompt_tokens_details": {"cached_tokens": 2},
				},
			},
			"m",
		)
		self.assertEqual(result.tool_calls[0].arguments, {"pattern": "x"})
		self.assertEqual(result.usage.output_tokens, 4)
		self.assertEqual((result.usage.input_tokens, result.usage.cached_tokens), (3, 2))


class TestParseJsonOuter(UnitTestCase):
	def test_outer_value_wins(self):
		from guard.llm.client import parse_json

		self.assertEqual(parse_json('[{"index": 0}]'), [{"index": 0}])
		self.assertEqual(parse_json('Answer: {"items": [1, 2]}'), {"items": [1, 2]})
		self.assertEqual(parse_json('```json\n[{"a": 1},]\n```'), [{"a": 1}])


class TestGatewayErrors(UnitTestCase):
	def post(self, status: int, calls: list[int] | None = None):
		import httpx

		from guard.llm.http import post_json

		def answer(*args, **kwargs):
			if calls is not None:
				calls.append(status)
			return httpx.Response(status, json={"error": {"message": "upstream account unpaid"}})

		with patch("guard.llm.http.httpx.post", side_effect=answer), patch("guard.llm.http.time.sleep"):
			return post_json("https://gateway.test/v1", {}, {}, 5)

	def test_lasting_5xx_is_gateway_down(self):
		from guard.llm.types import GatewayBusy, GatewayDown

		calls: list[int] = []
		with self.assertRaises(GatewayDown) as raised:
			self.post(502, calls)
		self.assertGreater(len(calls), 1)  # retried before giving up
		self.assertIsInstance(raised.exception, GatewayBusy)  # so a review waits, not fails
		self.assertIn("upstream account unpaid", str(raised.exception))

	def test_4xx_is_not_retried(self):
		from guard.llm.types import GatewayDown, LLMError

		calls: list[int] = []
		with self.assertRaises(LLMError) as raised:
			self.post(400, calls)
		self.assertNotIsInstance(raised.exception, GatewayDown)
		self.assertEqual(calls, [400])


class TestChatMany(UnitTestCase):
	def test_calls_run_together_answer_in_order_and_count_on_the_caller(self):
		# Each call waits for all the others, so the test passes only when they run at the same time.
		together = threading.Barrier(PARALLEL_CALLS, timeout=10)

		def respond(request: ChatRequest) -> ChatResult:
			# frappe.local reaches the thread: the flags, the config and the cache.
			self.assertTrue(frappe.flags.in_test)
			text = request.messages[-1].content
			if text == "bad":
				raise LLMError("no")
			if text != "alone":
				together.wait()
			return ChatResult(text, [], Usage(10, 1, 4))

		client = LLMClient(purpose="test")
		with use_responder(respond):
			results = client.chat_many([[Message("user", str(n))] for n in range(PARALLEL_CALLS)])
			self.assertEqual([r.content for r in results], [str(n) for n in range(PARALLEL_CALLS)])
			self.assertEqual(client.usage.input_tokens, 10 * PARALLEL_CALLS)
			self.assertEqual(client.usage.cached_tokens, 4 * PARALLEL_CALLS)
			with self.assertRaises(LLMError):
				client.chat_many([[Message("user", "bad")], [Message("user", "alone")]])
		self.assertEqual(client.usage.input_tokens, 10 * PARALLEL_CALLS + 10, "the call that answered counts")
