"""OpenAI-compatible chat completions, the default front of a Grove gateway."""

import json
from typing import Any

from guard.llm.http import get_json, post_json
from guard.llm.types import ChatResult, Message, ToolCall, ToolSpec, Usage


class OpenAIDialect:
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
		# No `reasoning_effort`: GPT-6 models refuse function tools on /v1/chat/completions at any
		# effort other than `none`, the default of the gateway.
		body: dict[str, Any] = {
			"model": model,
			"messages": [_message(m) for m in messages],
			"max_tokens": max_tokens,
			"temperature": temperature,
		}
		if tools:
			body["tools"] = [_tool(t) for t in tools]
			if tool_choice:
				body["tool_choice"] = (
					tool_choice
					if tool_choice in ("auto", "required", "none")
					else {"type": "function", "function": {"name": tool_choice}}
				)
		data = post_json(f"{self.url}/v1/chat/completions", body, self._headers(), self.timeout)
		return _result(data, model)

	def embed(self, model: str, texts: list[str]) -> list[list[float]]:
		data = post_json(
			f"{self.url}/v1/embeddings", {"model": model, "input": texts}, self._headers(), self.timeout
		)
		rows = sorted(data.get("data", []), key=lambda row: row.get("index", 0))
		return [row["embedding"] for row in rows]

	def models(self) -> list[str]:
		data = get_json(f"{self.url}/v1/models", self._headers())
		return [str(row["id"]) for row in data.get("data") or [] if row.get("id")]

	def _headers(self) -> dict[str, str]:
		return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}


def _message(message: Message) -> dict[str, Any]:
	if message.role == "tool":
		return {"role": "tool", "tool_call_id": message.tool_call_id, "content": message.content}
	data: dict[str, Any] = {"role": message.role, "content": message.content or ""}
	if message.tool_calls:
		data["tool_calls"] = [
			{
				"id": call.id,
				"type": "function",
				"function": {"name": call.name, "arguments": json.dumps(call.arguments)},
			}
			for call in message.tool_calls
		]
	return data


def _tool(tool: ToolSpec) -> dict[str, Any]:
	return {
		"type": "function",
		"function": {"name": tool.name, "description": tool.description, "parameters": tool.parameters},
	}


def _result(data: dict[str, Any], model: str) -> ChatResult:
	choice = (data.get("choices") or [{}])[0]
	message = choice.get("message") or {}
	calls = []
	for raw in message.get("tool_calls") or []:
		function = raw.get("function") or {}
		calls.append(ToolCall(raw.get("id") or "", function.get("name") or "", _arguments(function)))
	usage = data.get("usage") or {}
	cached = (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0
	return ChatResult(
		content=message.get("content") or "",
		tool_calls=calls,
		usage=Usage(usage.get("prompt_tokens") or 0, usage.get("completion_tokens") or 0, cached),
		stop_reason=choice.get("finish_reason") or "",
		model=data.get("model") or model,
	)


def _arguments(function: dict[str, Any]) -> dict[str, Any]:
	raw = function.get("arguments") or "{}"
	if isinstance(raw, dict):
		return raw
	try:
		value = json.loads(raw)
	except json.JSONDecodeError:
		return {"_raw": raw}
	return value if isinstance(value, dict) else {"_raw": value}
