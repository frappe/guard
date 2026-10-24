"""Provider-neutral message and tool types."""

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolSpec:
	name: str
	description: str
	parameters: dict[str, Any]


@dataclass
class ToolCall:
	id: str
	name: str
	arguments: dict[str, Any]


@dataclass
class Message:
	role: str  # system, user, assistant, tool
	content: str = ""
	tool_calls: list[ToolCall] = field(default_factory=list)
	tool_call_id: str | None = None
	# Ask the gateway to cache the prompt up to and including this message.
	cache: bool = False


@dataclass
class Usage:
	"""Token counts. `input_tokens` is the whole prompt; `cached_tokens` is the part of it read from the cache."""

	input_tokens: int = 0
	output_tokens: int = 0
	cached_tokens: int = 0

	def add(self, other: "Usage") -> None:
		self.input_tokens += other.input_tokens
		self.output_tokens += other.output_tokens
		self.cached_tokens += other.cached_tokens


@dataclass
class ChatResult:
	content: str
	tool_calls: list[ToolCall]
	usage: Usage
	stop_reason: str = ""
	model: str = ""

	def as_message(self) -> Message:
		return Message(role="assistant", content=self.content, tool_calls=list(self.tool_calls))


class LLMError(Exception):
	pass


class GatewayBusy(LLMError):
	"""The gateway asked us to come back later: rate limit (429) or spend cap (402)."""

	def __init__(self, message: str, retry_after: int = 60):
		super().__init__(message)
		self.retry_after = retry_after


class GatewayDown(GatewayBusy):
	"""The gateway kept failing after the retries: 5xx answers, or no connection.

	An outage of the gateway or its provider is not a fault of the review, so the review
	goes back to the queue, as for a rate limit.
	"""
