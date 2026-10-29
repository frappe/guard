"""Provider-neutral message and tool types."""

from dataclasses import dataclass, field
from typing import Any

# Stop reasons that mean the answer hit a limit and is cut short: Anthropic, then OpenAI.
TRUNCATED = ("max_tokens", "model_context_window_exceeded", "length")


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
	# Thinking blocks of the Anthropic front, as the model sent them. They go back unchanged, because
	# the API needs them in a tool turn.
	thinking: list[dict[str, Any]] = field(default_factory=list)


# The counts of each purpose in `Usage.purposes`.
PURPOSE_COUNTS = ("calls", "input", "cached", "output", "reasoning")


@dataclass
class Usage:
	"""Token counts. `input_tokens` is the whole prompt; `cached_tokens` is the part of it read from the cache.

	`reasoning_tokens` is the part of `output_tokens` that the model thought, when the provider tells it.
	`purposes` holds the counts of each purpose of a call (review, verify, stale, ...), and `tools`
	the calls and the characters of output of each agent tool, so that a review shows where its
	tokens went.
	"""

	input_tokens: int = 0
	output_tokens: int = 0
	cached_tokens: int = 0
	reasoning_tokens: int = 0
	purposes: dict[str, dict[str, int]] = field(default_factory=dict)
	tools: dict[str, dict[str, int]] = field(default_factory=dict)

	def add(self, other: "Usage", purpose: str = "") -> None:
		"""Add the counts of `other`. With `purpose`, they also count for that purpose: give it only for
		the usage of one call, which has no purposes of its own, else they count twice."""
		self.input_tokens += other.input_tokens
		self.output_tokens += other.output_tokens
		self.cached_tokens += other.cached_tokens
		self.reasoning_tokens += other.reasoning_tokens
		if purpose:
			counts = (1, other.input_tokens, other.cached_tokens, other.output_tokens, other.reasoning_tokens)
			_add_counts(self.purposes, purpose, dict(zip(PURPOSE_COUNTS, counts, strict=True)))
		for name, row in other.purposes.items():
			_add_counts(self.purposes, name, row)
		for name, row in other.tools.items():
			_add_counts(self.tools, name, row)

	def tool(self, name: str, chars: int) -> None:
		"""Count one call of an agent tool that returned `chars` characters."""
		_add_counts(self.tools, name, {"calls": 1, "chars": chars})

	def detail(self) -> dict[str, Any]:
		"""The counts that the columns of a Code Review do not hold, for its `usage_detail`."""
		return {"reasoning_tokens": self.reasoning_tokens, "purposes": self.purposes, "tools": self.tools}

	@classmethod
	def from_detail(cls, detail: Any) -> "Usage":
		"""The counts of a saved `detail()`, without the totals that are in their own columns."""
		detail = detail if isinstance(detail, dict) else {}
		usage = cls(reasoning_tokens=_int(detail.get("reasoning_tokens")))
		for key, target in (("purposes", usage.purposes), ("tools", usage.tools)):
			rows = detail.get(key)
			for name, row in (rows if isinstance(rows, dict) else {}).items():
				if isinstance(row, dict):
					_add_counts(target, str(name), {str(k): _int(v) for k, v in row.items()})
		return usage


def _add_counts(rows: dict[str, dict[str, int]], name: str, counts: dict[str, int]) -> None:
	row = rows.setdefault(name, {})
	for key, value in counts.items():
		row[key] = row.get(key, 0) + value


def _int(value: Any) -> int:
	try:
		return int(value or 0)
	except TypeError, ValueError:
		return 0


@dataclass
class ChatResult:
	content: str
	tool_calls: list[ToolCall]
	usage: Usage
	stop_reason: str = ""
	model: str = ""
	thinking: list[dict[str, Any]] = field(default_factory=list)

	@property
	def truncated(self) -> bool:
		return self.stop_reason in TRUNCATED

	def as_message(self) -> Message:
		return Message(
			role="assistant", content=self.content, tool_calls=list(self.tool_calls), thinking=self.thinking
		)


class LLMError(Exception):
	# The AI provider that failed. The client sets it, so the breaker pauses only that provider.
	ai_provider: str = ""


class GatewayBusy(LLMError):
	"""The gateway asked us to come back later: rate limit (429) or spend cap (402)."""

	def __init__(self, message: str, retry_after: int = 60):
		super().__init__(message)
		self.retry_after = retry_after


class GatewayBilling(GatewayBusy):
	"""The gateway refused the key for money (402): credit is used up, or a spend cap is reached.

	A review waits, as for a rate limit, because someone can add credit. An eval stops at once.
	"""


class GatewayDown(GatewayBusy):
	"""The gateway kept failing after the retries: 5xx answers, or no connection.

	An outage of the gateway or its provider is not a fault of the review, so the review
	goes back to the queue, as for a rate limit.
	"""
