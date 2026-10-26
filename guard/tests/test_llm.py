import threading
from unittest.mock import patch

import frappe
from frappe.tests import UnitTestCase

from guard.llm import markup
from guard.llm.anthropic_dialect import VERSION as ANTHROPIC_VERSION
from guard.llm.anthropic_dialect import AnthropicDialect, _messages
from guard.llm.anthropic_dialect import _result as anthropic_result
from guard.llm.client import PARALLEL_CALLS, ChatRequest, LLMClient, parse_json, use_responder
from guard.llm.embeddings import hash_embedding, tokens
from guard.llm.offline import OfflineDialect
from guard.llm.openai_dialect import OpenAIDialect
from guard.llm.openai_dialect import _result as openai_result
from guard.llm.types import (
	ChatResult,
	GatewayBilling,
	GatewayBusy,
	GatewayDown,
	LLMError,
	Message,
	ToolCall,
	ToolSpec,
	Usage,
)
from guard.tests.fakes import dsml

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

	def test_models_use_the_auth_of_the_dialect(self):
		import httpx

		seen: list[tuple[str, dict]] = []

		def answer(url, headers, timeout):
			seen.append((url, headers))
			return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": ""}]})

		with patch("guard.llm.http.httpx.get", side_effect=answer):
			self.assertEqual(AnthropicDialect("https://gw", "k", 30).models(), ["m1"])
			self.assertEqual(OpenAIDialect("https://gw", "k", 30).models(), ["m1"])
		self.assertEqual(seen[0][1], {"x-api-key": "k", "anthropic-version": ANTHROPIC_VERSION})
		self.assertTrue(seen[0][0].startswith("https://gw/v1/models?"))
		self.assertEqual(seen[1], ("https://gw/v1/models", {"Authorization": "Bearer k"}))


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
		calls: list[int] = []
		with self.assertRaises(GatewayDown) as raised:
			self.post(502, calls)
		self.assertGreater(len(calls), 1)  # retried before giving up
		self.assertIsInstance(raised.exception, GatewayBusy)  # so a review waits, not fails
		self.assertIn("upstream account unpaid", str(raised.exception))

	def test_402_is_a_billing_error_and_429_is_not(self):
		with self.assertRaises(GatewayBilling):
			self.post(402)
		with self.assertRaises(GatewayBusy) as raised:
			self.post(429)
		self.assertNotIsInstance(raised.exception, GatewayBilling)

	def test_hung_call_gives_up_after_two_timeouts(self):
		import httpx

		from guard.llm.http import post_json

		# CR-000519: a turn waited 8 minutes. Seven timeouts of 300 s would hold a review for 35.
		clock = [0.0]
		timeouts: list[float] = []

		def hang(*args, **kwargs):
			timeouts.append(kwargs["timeout"])
			clock[0] += kwargs["timeout"]
			raise httpx.ReadTimeout("no answer")

		with (
			patch("guard.llm.http.httpx.post", side_effect=hang),
			patch("guard.llm.http.time.sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)),
			patch("guard.llm.http.time.monotonic", side_effect=lambda: clock[0]),
		):
			with self.assertRaises(GatewayDown):
				post_json("https://gateway.test/v1", {}, {}, 300)
		# The retry gets the rest of the budget, not a full timeout.
		self.assertEqual(timeouts, [300, 299])  # 1 s went to the backoff

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

	def test_calls_that_a_busy_gateway_refuses_go_again_one_at_a_time(self):
		lock = threading.Lock()
		refused: list[str] = []

		def respond(request: ChatRequest) -> ChatResult:
			text = request.messages[-1].content
			with lock:
				if text in ("1", "2") and text not in refused:
					refused.append(text)
					raise GatewayBusy("429", 5)
			return ChatResult(text, [], Usage(10, 1))

		client = LLMClient(purpose="test")
		with use_responder(respond):
			results = client.chat_many([[Message("user", str(n))] for n in range(4)])
		self.assertEqual([r.content for r in results], ["0", "1", "2", "3"])
		self.assertEqual(sorted(refused), ["1", "2"])
		self.assertEqual(client.parallel, 1, "the later calls of the client go one at a time")
		self.assertEqual(client.usage.input_tokens, 40)

	def test_a_down_or_unpaid_gateway_is_not_asked_again(self):
		for error in (GatewayDown("down"), GatewayBilling("402")):
			calls: list[str] = []

			def respond(request: ChatRequest, error: Exception = error) -> ChatResult:
				calls.append(request.messages[-1].content)
				raise error

			with use_responder(respond), self.assertRaises(type(error)):
				LLMClient(purpose="test").chat_many([[Message("user", "a")], [Message("user", "b")]])
			self.assertEqual(len(calls), 2)


READ = ToolSpec("read_file", "Read a file", {"type": "object", "properties": {"path": {"type": "string"}}})
LEAKED = dsml([("read_file", {"path": "app/a.py", "ranges": [{"start_line": 1, "end_line": 9}]})])


class TestMarkup(UnitTestCase):
	"""DeepSeek tool calls that came back as text (frappe/erpnext#62211, frappe/suite#1081)."""

	def chat(self, content: str, tools: list[ToolSpec] | None) -> ChatResult:
		with use_responder(lambda request: ChatResult(content, [], Usage(10, 5))):
			return LLMClient(purpose="test").chat([Message("user", "q")], tools=tools)

	def test_calls_in_text_become_tool_calls(self):
		result = self.chat(LEAKED, [READ])
		self.assertEqual(result.content, "")
		self.assertEqual([call.name for call in result.tool_calls], ["read_file"])
		# A string stays a string, and other values are JSON.
		self.assertEqual(
			result.tool_calls[0].arguments,
			{"path": "app/a.py", "ranges": [{"start_line": 1, "end_line": 9}]},
		)
		self.assertTrue(result.tool_calls[0].id)

	def test_markup_is_removed_when_no_tools_were_offered(self):
		result = self.chat(f"Partly checked.\n\n{LEAKED}", None)
		self.assertEqual((result.content, result.tool_calls), ("Partly checked.", []))

	def test_only_offered_tools_are_called(self):
		result = self.chat(dsml([("write_file", {"path": "x"}), ("read_file", {"path": "y"})]), [READ])
		self.assertEqual([call.name for call in result.tool_calls], ["read_file"])

	def test_the_documented_tag_and_a_block_that_max_tokens_cut_off(self):
		self.assertEqual(
			len(
				markup.recover(
					ChatResult(dsml([("read_file", {"path": "a"})], "function_calls"), [], Usage()), [READ]
				).tool_calls
			),
			1,
		)
		cut = LEAKED.rsplit("</", 1)[0]
		result = self.chat(cut, [READ])
		self.assertEqual((result.content, len(result.tool_calls)), ("", 1))

	def test_real_tool_calls_and_plain_text_are_left_alone(self):
		call = ToolCall("t1", "read_file", {"path": "a"})
		with use_responder(lambda request: ChatResult(LEAKED, [call], Usage())):
			result = LLMClient(purpose="test").chat([Message("user", "q")], tools=[READ])
		self.assertEqual((result.content, result.tool_calls), ("", [call]))
		self.assertEqual(self.chat("Use `a|b` here.", [READ]).content, "Use `a|b` here.")
