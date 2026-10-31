"""Typed access to Guard Settings."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe.utils import cint, flt, get_url

if TYPE_CHECKING:
	from guard.guard.doctype.guard_settings.guard_settings import GuardSettings

TIERS = ("base", "plus", "apex")
# Each model field of Guard Settings: one for each tier, then the fast and the embedding model.
# Each can name its own AI provider, in `<slot>_ai_provider`.
SLOTS = (*TIERS, "fast", "embedding")
DEFAULT_TIMEOUT = 300  # seconds for one request to an AI provider
CREDITS = {"base": 1, "plus": 3, "apex": 10}
# Every turn sends the whole conversation again, so the cost of a review grows with the
# square of its turns. The model can ask for several tools in one turn.
DEFAULT_TURNS = {"base": 10, "plus": 16, "apex": 24}
DEFAULT_CONCURRENT_REVIEWS = 4
# Seconds, long enough for the fix-up pushes that often come right after a push.
DEFAULT_PUSH_REVIEW_DELAY = 120
# A short blip of an AI provider is not worth a review without a model. 15 minutes is longer than most.
DEFAULT_OUTAGE_FALLBACK = 900
# Input tokens of the automatic reviews of one pull request in 24 hours. Only a few pull requests pass
# it: they had 10 to 50 million in a day, mostly in re-reviews that posted little.
DEFAULT_PULL_REQUEST_TOKENS = 10_000_000
# Threads of one worker. Each holds a database connection, and a hung one keeps its slot.
MAX_REVIEWS_PER_WORKER = 16


@dataclass
class AIProviderConfig:
	"""One AI provider: a gateway such as Grove, or a vendor API such as OpenRouter or Anthropic."""

	name: str
	url: str
	api_key: str = ""
	dialect: str = "openai"
	timeout: int = DEFAULT_TIMEOUT


@dataclass
class Route:
	"""A model and the AI provider that serves it. Without both, Guard works offline. In offline
	mode, no route has an AI provider."""

	model: str
	ai_provider: AIProviderConfig | None

	@property
	def online(self) -> bool:
		return bool(self.model and self.ai_provider and self.ai_provider.url)

	@property
	def ai_provider_name(self) -> str:
		"""The breaker and the errors use it. Blank when there is no AI provider."""
		return self.ai_provider.name if self.ai_provider else ""


@dataclass
class GatewayConfig:
	"""The models of Guard Settings, and the enabled AI providers that serve them."""

	models: dict[str, str] = field(default_factory=dict)
	ai_providers: dict[str, AIProviderConfig] = field(default_factory=dict)
	default_ai_provider: str = ""
	# The AI provider of each slot of SLOTS. Blank means the default AI provider.
	slot_ai_providers: dict[str, str] = field(default_factory=dict)
	fast_model: str = ""
	embedding_model: str = ""
	embedding_dimensions: int = 1024
	offline: bool = False

	@property
	def enabled(self) -> bool:
		return self.route("base").online

	def model_for(self, tier: str) -> str:
		"""Model for a tier. Falls back to the next lower tier that has a model."""
		return self.route(tier).model

	def route(self, slot: str) -> Route:
		"""The model of a slot and its AI provider.

		A tier without a model uses the model of the next lower tier that has one, with the AI
		provider of that tier. Without a fast model, the fast slot uses the base tier.
		"""
		if slot == "fast":
			return (
				Route(self.fast_model, self.ai_provider_for(slot)) if self.fast_model else self.route("base")
			)
		if slot == "embedding":
			return Route(self.embedding_model, self.ai_provider_for(slot))
		order = TIERS[: TIERS.index(slot) + 1] if slot in TIERS else ("base",)
		for name in reversed(order):
			if self.models.get(name):
				return Route(self.models[name], self.ai_provider_for(name))
		return Route("", self.ai_provider_for("base"))

	def ai_provider_for(self, slot: str) -> AIProviderConfig | None:
		"""The AI provider of a slot. None when it is not set or is disabled."""
		return self.ai_providers.get(self.slot_ai_providers.get(slot) or self.default_ai_provider)


def settings() -> "GuardSettings":
	return cast("GuardSettings", frappe.get_cached_doc("Guard Settings"))


def gateway() -> GatewayConfig:
	doc = settings()
	slot_ai_providers = {slot: str(doc.get(f"{slot}_ai_provider") or "") for slot in SLOTS}
	default_ai_provider = doc.default_ai_provider or ""
	offline = bool(doc.offline_mode)
	return GatewayConfig(
		models={"base": doc.base_model or "", "plus": doc.plus_model or "", "apex": doc.apex_model or ""},
		# Only those that a model can use: each has a key to decrypt. Offline, none: every route is offline.
		ai_providers={} if offline else ai_providers([default_ai_provider, *slot_ai_providers.values()]),
		default_ai_provider=default_ai_provider,
		slot_ai_providers=slot_ai_providers,
		fast_model=doc.fast_model or "",
		embedding_model=doc.embedding_model or "",
		embedding_dimensions=cint(doc.embedding_dimensions) or 1024,
		offline=offline,
	)


def offline() -> bool:
	"""Offline mode is on: Guard makes no calls to AI providers."""
	return flag("offline_mode")


def ai_providers(names: Iterable[str]) -> dict[str, AIProviderConfig]:
	"""The enabled AI providers of `names`, by name. Not cached: an admin can change a key at any time."""
	from frappe.utils.password import get_decrypted_password

	names = [name for name in set(names) if name]
	if not names:
		return {}
	timeout = request_timeout()
	rows = frappe.get_all(
		"AI Provider", filters={"enabled": 1, "name": ["in", names]}, fields=["name", "url", "dialect"]
	)
	return {
		row.name: AIProviderConfig(
			name=row.name,
			url=(row.url or "").rstrip("/"),
			api_key=str(get_decrypted_password("AI Provider", row.name, "api_key", False) or ""),
			dialect=row.dialect or "openai",
			timeout=timeout,
		)
		for row in rows
	}


def request_timeout() -> int:
	"""Seconds for one request to an AI provider."""
	return cint(_value("request_timeout")) or DEFAULT_TIMEOUT


@dataclass
class Prices:
	"""USD for one million tokens of one model. Only for cost estimates: the gateway bills."""

	input: float = 0.0
	cached_input: float = 0.0
	output: float = 0.0

	@property
	def known(self) -> bool:
		return bool(self.input or self.cached_input or self.output)

	def cost(self, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
		"""`input_tokens` is the whole prompt, and `cached_tokens` is the part of it that the cache covered."""
		uncached = max(input_tokens - cached_tokens, 0)
		return (uncached * self.input + cached_tokens * self.cached_input + output_tokens * self.output) / 1e6


@dataclass
class PriceList:
	"""The prices of each tier, from Guard Settings."""

	tiers: dict[str, Prices] = field(default_factory=dict)

	@property
	def known(self) -> bool:
		return any(prices.known for prices in self.tiers.values())

	def for_tier(self, tier: str) -> Prices:
		"""Prices for a tier. Falls back to the next lower tier that has prices, as `model_for` does
		for the model. A review with no tier yet counts as base."""
		order = TIERS[: TIERS.index(tier) + 1] if tier in TIERS else ("base",)
		for name in reversed(order):
			if (prices := self.tiers.get(name)) and prices.known:
				return prices
		return Prices()

	def cost(self, tier: str, input_tokens: int, cached_tokens: int, output_tokens: int) -> float:
		return self.for_tier(tier).cost(input_tokens, cached_tokens, output_tokens)


def prices() -> PriceList:
	doc = settings()
	return PriceList(
		{
			"base": Prices(
				flt(doc.base_input_price), flt(doc.base_cached_input_price), flt(doc.base_output_price)
			),
			"plus": Prices(
				flt(doc.plus_input_price), flt(doc.plus_cached_input_price), flt(doc.plus_output_price)
			),
			"apex": Prices(
				flt(doc.apex_input_price), flt(doc.apex_cached_input_price), flt(doc.apex_output_price)
			),
		}
	)


def max_turns(tier: str) -> int:
	return cint(_value(f"max_turns_{tier}")) or DEFAULT_TURNS.get(tier, 15)


def max_concurrent_reviews() -> int:
	"""Reviews that may run at once. 0 means no limit."""
	value = _value("max_concurrent_reviews")
	return DEFAULT_CONCURRENT_REVIEWS if value in (None, "") else max(cint(value), 0)


def reviews_per_worker() -> int:
	"""Reviews that one worker runs at once, on threads. 1 runs each review in its own job."""
	return min(max(cint(_value("reviews_per_worker")), 1), MAX_REVIEWS_PER_WORKER)


def push_review_delay() -> int:
	"""Seconds that an automatic review of a push waits, so that a newer push can replace it first."""
	value = _value("push_review_delay")
	return DEFAULT_PUSH_REVIEW_DELAY if value in (None, "") else max(cint(value), 0)


def outage_fallback_after() -> int:
	"""Seconds of an outage after which a review runs without a model. 0 means reviews wait."""
	value = _value("outage_fallback_after")
	return DEFAULT_OUTAGE_FALLBACK if value in (None, "") else max(cint(value), 0)


def pull_request_daily_tokens() -> int:
	"""Input tokens that the reviews of one pull request can use in 24 hours, before a push gets no
	automatic review. 0 is no limit."""
	value = _value("pull_request_daily_tokens")
	return DEFAULT_PULL_REQUEST_TOKENS if value in (None, "") else max(cint(value), 0)


def repository_daily_reviews() -> int:
	"""Reviews of pull requests of one repository that can start in 24 hours. 0 is no limit."""
	return max(cint(_value("repository_daily_reviews")), 0)


def trigger_handles() -> list[str]:
	"""Configured handles, plus `@<slug>` of each GitHub App, which is what GitHub suggests."""
	handles = _lines(settings().trigger_handles or "@guard")
	slugs = frappe.get_all(
		"Code Provider", filters={"enabled": 1, "app_slug": ["is", "set"]}, pluck="app_slug"
	)
	return list(dict.fromkeys([*handles, *(f"@{slug}" for slug in slugs)]))


def bot_logins() -> set[str]:
	"""Configured logins, plus the login of each provider: `<slug>[bot]` for a GitHub App, which is
	how GitHub names the author of its comments, and the bot username for a token."""
	logins = _lines(settings().bot_logins or "")
	for provider in frappe.get_all(
		"Code Provider", filters={"enabled": 1}, fields=["app_slug", "bot_username"]
	):
		if provider.app_slug:
			logins.append(f"{provider.app_slug}[bot]")
		if provider.bot_username:
			logins.append(provider.bot_username)
	return {login.lower() for login in logins}


def public_url(path: str = "") -> str:
	base = (settings().public_url or get_url()).rstrip("/")
	return f"{base}{path}"


def flag(name: str) -> bool:
	return bool(cint(_value(name)))


def _value(name: str) -> Any:
	return getattr(settings(), name, None)


def _lines(text: str) -> list[str]:
	return [line.strip() for line in text.splitlines() if line.strip()]
