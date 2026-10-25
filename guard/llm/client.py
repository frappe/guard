"""The one LLM interface that the rest of Guard uses."""

import contextvars
import json
import re
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from guard import settings as guard_settings
from guard.llm.anthropic_dialect import AnthropicDialect
from guard.llm.offline import OfflineDialect
from guard.llm.openai_dialect import OpenAIDialect
from guard.llm.types import ChatResult, GatewayBilling, GatewayBusy, GatewayDown, Message, ToolSpec, Usage


@dataclass
class ChatRequest:
	model: str
	messages: list[Message]
	tools: list[ToolSpec] | None
	tool_choice: str | None
	purpose: str


Responder = Callable[[ChatRequest], ChatResult]
_responder: Responder | None = None
# Model calls of one job in flight at once. A review holds one of `max_concurrent_reviews`
# slots, so the gateway sees at most this many calls for each slot.
PARALLEL_CALLS = 4


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
		self.parallel = PARALLEL_CALLS

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
		return self._record([self._call(messages, tools, tool_choice, max_tokens, temperature)])[0]

	def chat_many(
		self,
		conversations: list[list[Message]],
		tools: list[ToolSpec] | None = None,
		tool_choice: str | None = None,
		max_tokens: int = 4096,
		temperature: float = 0.1,
	) -> list[ChatResult]:
		"""Send several conversations at once, and answer in the same order.

		Only the gateway calls run on threads. The breaker and the token count stay on this thread,
		because they can reach `frappe.db`, which is not thread-safe. When a call fails, the others
		still finish and count, and then the first error is raised.

		When the gateway refuses calls as busy, they go again one at a time, and so do the later
		calls of this client: the answers already in are kept, and the load on the gateway drops.
		"""

		def call(messages: list[Message]) -> ChatResult | Exception:
			try:
				return self._call(messages, tools, tool_choice, max_tokens, temperature)
			except Exception as error:
				return error

		if len(conversations) <= 1 or self.parallel <= 1:
			return [self.chat(m, tools, tool_choice, max_tokens, temperature) for m in conversations]
		with ThreadPoolExecutor(min(self.parallel, len(conversations))) as pool:
			# A copy of the context gives each thread `frappe.local`: the site config, flags and cache.
			futures = [pool.submit(contextvars.copy_context().run, call, m) for m in conversations]
			outcomes = [future.result() for future in futures]
		for index, outcome in enumerate(outcomes):
			# Not GatewayDown: the calls already outlasted their retries, and the breaker handles it.
			# Not GatewayBilling: a refused key stays refused, one call at a time or not.
			if isinstance(outcome, GatewayBusy) and not isinstance(outcome, GatewayDown | GatewayBilling):
				self.parallel = 1
				outcomes[index] = call(conversations[index])
		results = self._record([o for o in outcomes if isinstance(o, ChatResult)])
		error = next((o for o in outcomes if isinstance(o, Exception)), None)
		if error:
			raise error
		return results

	def _call(
		self,
		messages: list[Message],
		tools: list[ToolSpec] | None,
		tool_choice: str | None,
		max_tokens: int,
		temperature: float,
	) -> ChatResult:
		if _responder:
			return _responder(ChatRequest(self.model, messages, tools, tool_choice, self.purpose))
		return self.dialect.chat(self.model, messages, tools, tool_choice, max_tokens, temperature)

	def _record(self, results: list[ChatResult]) -> list[ChatResult]:
		if results and self.online:
			from guard.llm import breaker

			breaker.record_success()
		for result in results:
			self.usage.add(result.usage)
		return results

	def complete(self, system: str, prompt: str, max_tokens: int = 2048) -> str:
		messages = [Message("system", system), Message("user", prompt)]
		return self.chat(messages, max_tokens=max_tokens).content

	def complete_json(self, system: str, prompt: str, default: Any, max_tokens: int = 4096) -> Any:
		"""Ask for JSON. Return `default` when the answer has no valid JSON."""
		text = self.complete(system + "\n\nAnswer with JSON only.", prompt, max_tokens)
		value = parse_json(text)
		return default if value is None else value

	def complete_json_many(
		self, system: str, prompts: list[str], default: Any, max_tokens: int = 4096
	) -> list[Any]:
		"""`complete_json` for several prompts at once, with the same system prompt."""
		system = system + "\n\nAnswer with JSON only."
		results = self.chat_many(
			[[Message("system", system), Message("user", prompt)] for prompt in prompts],
			max_tokens=max_tokens,
		)
		values = [parse_json(result.content) for result in results]
		return [default if value is None else value for value in values]

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
