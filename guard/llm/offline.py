"""Deterministic stand-in for the gateway. Used when no gateway is set, and in tests."""

from typing import Any

from guard.llm.types import ChatResult, Message, ToolCall, ToolSpec, Usage


class OfflineDialect:
	"""Never invents findings. A forced tool call gets the schema defaults."""

	def chat(
		self,
		model: str,
		messages: list[Message],
		tools: list[ToolSpec] | None,
		tool_choice: str | None,
		max_tokens: int,
		temperature: float,
		effort: str,
	) -> ChatResult:
		forced = next((t for t in tools or [] if t.name == tool_choice), None)
		if forced:
			call = ToolCall(id="offline-1", name=forced.name, arguments=schema_defaults(forced.parameters))
			return ChatResult(content="", tool_calls=[call], usage=Usage(), model="offline")
		return ChatResult(content="", tool_calls=[], usage=Usage(), stop_reason="stop", model="offline")


def schema_defaults(schema: dict[str, Any]) -> dict[str, Any]:
	"""Fill required properties with empty values of their type."""
	properties = schema.get("properties") or {}
	values: dict[str, Any] = {}
	for name in schema.get("required") or []:
		values[name] = _empty(properties.get(name) or {})
	return values


def _empty(prop: dict[str, Any]) -> Any:
	if "default" in prop:
		return prop["default"]
	if prop.get("enum"):
		return prop["enum"][-1]
	kind = prop.get("type")
	return {"string": "", "integer": 0, "number": 0, "boolean": False, "array": [], "object": {}}.get(
		kind if isinstance(kind, str) else "string", ""
	)
