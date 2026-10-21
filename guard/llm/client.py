"""The one LLM interface that the rest of Guard uses."""

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from guard import settings as guard_settings
from guard.llm.anthropic_dialect import AnthropicDialect
from guard.llm.offline import OfflineDialect
from guard.llm.openai_dialect import OpenAIDialect
from guard.llm.types import ChatResult, Message, ToolSpec, Usage


@dataclass
class ChatRequest:
	model: str
	messages: list[Message]
	tools: list[ToolSpec] | None
	tool_choice: str | None
	purpose: str


Responder = Callable[[ChatRequest], ChatResult]
_responder: Responder | None = None


class Dialect(Protocol):
	def chat(
		self,
		model: str,
		messages: list[Message],
		tools: list[ToolSpec] | None,
		tool_choice: str | None,
		max_tokens: int,
		temperature: float,
	) -> ChatResult: ...


@contextmanager
def use_responder(responder: Responder) -> Iterator[None]:
	"""Answer every chat call with `responder`. Tests use this."""
	global _responder
	previous, _responder = _responder, responder
	try:
		yield
	finally:
		_responder = previous


class LLMClient:
	"""Chat with one model through the gateway. Adds token use to `usage`."""

	def __init__(
		self, tier: str = "base", model: str | None = None, purpose: str = "", usage: Usage | None = None
	):
		self.config = guard_settings.gateway()
		self.model = model or self.config.model_for(tier)
		self.purpose = purpose
		self.usage = usage if usage is not None else Usage()
		self.dialect = self._dialect()

	@classmethod
	def fast(cls, purpose: str = "", usage: Usage | None = None) -> "LLMClient":
		config = guard_settings.gateway()
		return cls(model=config.fast_model or config.model_for("base"), purpose=purpose, usage=usage)

	@property
	def online(self) -> bool:
		return not isinstance(self.dialect, OfflineDialect)

	def chat(
		self,
		messages: list[Message],
		tools: list[ToolSpec] | None = None,
		tool_choice: str | None = None,
		max_tokens: int = 4096,
		temperature: float = 0.1,
	) -> ChatResult:
		if _responder:
			result = _responder(ChatRequest(self.model, messages, tools, tool_choice, self.purpose))
		else:
			result = self.dialect.chat(self.model, messages, tools, tool_choice, max_tokens, temperature)
		self.usage.add(result.usage)
		return result

	def complete(self, system: str, prompt: str, max_tokens: int = 2048) -> str:
		messages = [Message("system", system), Message("user", prompt)]
		return self.chat(messages, max_tokens=max_tokens).content

	def complete_json(self, system: str, prompt: str, default: Any, max_tokens: int = 4096) -> Any:
		"""Ask for JSON. Return `default` when the answer has no valid JSON."""
		text = self.complete(system + "\n\nAnswer with JSON only.", prompt, max_tokens)
		value = parse_json(text)
		return default if value is None else value

	def _dialect(self) -> Dialect:
		config = self.config
		if not (config.url and self.model):
			return OfflineDialect()
		if config.dialect == "anthropic":
			return AnthropicDialect(config.url, config.api_key, config.timeout)
		return OpenAIDialect(config.url, config.api_key, config.timeout)


def _position(text: str, char: str) -> int:
	index = text.find(char)
	return index if index != -1 else len(text)


def parse_json(text: str) -> Any:
	"""Find and parse the first JSON object or array in model output."""
	if not text:
		return None
	fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
	candidates = [fenced.group(1)] if fenced else []
	candidates.append(text)
	for candidate in candidates:
		# The bracket that opens first is the outer value: `[{...}]` is a list, not its item.
		pairs = sorted((("{", "}"), ("[", "]")), key=lambda pair: _position(candidate, pair[0]))
		for opener, closer in pairs:
			start, end = candidate.find(opener), candidate.rfind(closer)
			if start == -1 or end <= start:
				continue
			snippet = candidate[start : end + 1]
			for attempt in (snippet, re.sub(r",\s*([}\]])", r"\1", snippet)):
				try:
					return json.loads(attempt)
				except json.JSONDecodeError:
					continue
	return None
