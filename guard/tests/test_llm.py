from frappe.tests import UnitTestCase

from guard.llm.anthropic_dialect import _messages
from guard.llm.anthropic_dialect import _result as anthropic_result
from guard.llm.client import parse_json
from guard.llm.embeddings import hash_embedding, tokens
from guard.llm.offline import OfflineDialect
from guard.llm.openai_dialect import _result as openai_result
from guard.llm.types import Message, ToolCall, ToolSpec

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

	def test_anthropic_result(self):
		result = anthropic_result(
			{
				"content": [
					{"type": "text", "text": "ok"},
					{"type": "tool_use", "id": "x", "name": "grep", "input": {"pattern": "a"}},
				],
				"usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 7},
			},
			"m",
		)
		self.assertEqual(result.content, "ok")
		self.assertEqual(result.tool_calls[0].arguments, {"pattern": "a"})
		self.assertEqual(result.usage.cached_tokens, 7)

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
				"usage": {"prompt_tokens": 3, "completion_tokens": 4},
			},
			"m",
		)
		self.assertEqual(result.tool_calls[0].arguments, {"pattern": "x"})
		self.assertEqual(result.usage.output_tokens, 4)


class TestParseJsonOuter(UnitTestCase):
	def test_outer_value_wins(self):
		from guard.llm.client import parse_json

		self.assertEqual(parse_json('[{"index": 0}]'), [{"index": 0}])
		self.assertEqual(parse_json('Answer: {"items": [1, 2]}'), {"items": [1, 2]})
		self.assertEqual(parse_json('```json\n[{"a": 1},]\n```'), [{"a": 1}])
