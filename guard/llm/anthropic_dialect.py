"""Anthropic-compatible messages API, the second front of a Grove gateway.

Supports prompt caching: a message with `cache=True` gets a cache breakpoint.
"""

from typing import Any

from guard.llm.http import post_json
from guard.llm.types import ChatResult, Message, ToolCall, ToolSpec, Usage

VERSION = "2023-06-01"


class AnthropicDialect:
	def __init__(self, url: str, api_key: str, timeout: int):
		self.url = url
		self.api_key = api_key
		self.timeout = timeout

	def chat(
		self,
		model: str,
		messages: list[Message],
		tools: list[ToolSpec] | None,
		tool_choice: str | None,
		max_tokens: int,
		temperature: float,
	) -> ChatResult:
		system = [m for m in messages if m.role == "system"]
		body: dict[str, Any] = {
			"model": model,
			"max_tokens": max_tokens,
			"temperature": temperature,
			"messages": _messages([m for m in messages if m.role != "system"]),
		}
		if system:
			body["system"] = [_text_block(m.content, m.cache) for m in system]
		if tools:
			body["tools"] = [
				{"name": t.name, "description": t.description, "input_schema": t.parameters} for t in tools
			]
			if tool_choice and tool_choice not in ("auto", "none"):
				body["tool_choice"] = (
					{"type": "any"} if tool_choice == "required" else {"type": "tool", "name": tool_choice}
				)
		headers = {"x-api-key": self.api_key, "anthropic-version": VERSION}
		data = post_json(f"{self.url}/v1/messages", body, headers, self.timeout)
		return _result(data, model)

	def embed(self, model: str, texts: list[str]) -> list[list[float]]:
		raise NotImplementedError("The Anthropic front has no embeddings. Use the OpenAI front.")


def _text_block(text: str, cache: bool) -> dict[str, Any]:
	block: dict[str, Any] = {"type": "text", "text": text or " "}
	if cache:
		block["cache_control"] = {"type": "ephemeral"}
	return block


def _messages(messages: list[Message]) -> list[dict[str, Any]]:
	"""Convert to Anthropic blocks. Consecutive tool results merge into one user turn."""
	out: list[dict[str, Any]] = []
	for message in messages:
		if message.role == "tool":
			block: dict[str, Any] = {
				"type": "tool_result",
				"tool_use_id": message.tool_call_id,
				"content": message.content,
			}
			if message.cache:
				block["cache_control"] = {"type": "ephemeral"}
			if out and out[-1]["role"] == "user" and _is_tool_results(out[-1]):
				out[-1]["content"].append(block)
			else:
				out.append({"role": "user", "content": [block]})
			continue
		content: list[dict[str, Any]] = []
		if message.content:
			content.append(_text_block(message.content, message.cache))
		for call in message.tool_calls:
			content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
		out.append({"role": message.role, "content": content or [_text_block(" ", False)]})
	return out


def _is_tool_results(message: dict[str, Any]) -> bool:
	return all(block.get("type") == "tool_result" for block in message["content"])


def _result(data: dict[str, Any], model: str) -> ChatResult:
	text, calls = [], []
	for block in data.get("content") or []:
		if block.get("type") == "text":
			text.append(block.get("text") or "")
		elif block.get("type") == "tool_use":
			calls.append(ToolCall(block.get("id") or "", block.get("name") or "", block.get("input") or {}))
	usage = data.get("usage") or {}
	# Anthropic counts the cached and the newly cached parts apart from `input_tokens`. Add them, so
	# that the input is the whole prompt, as on the OpenAI front.
	cached = usage.get("cache_read_input_tokens") or 0
	prompt = (usage.get("input_tokens") or 0) + cached + (usage.get("cache_creation_input_tokens") or 0)
	return ChatResult(
		content="".join(text),
		tool_calls=calls,
		usage=Usage(prompt, usage.get("output_tokens") or 0, cached),
		stop_reason=data.get("stop_reason") or "",
		model=data.get("model") or model,
	)
