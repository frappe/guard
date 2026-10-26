"""Typed access to Guard Settings."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe.utils import cint, flt, get_url

if TYPE_CHECKING:
	from guard.guard.doctype.guard_settings.guard_settings import GuardSettings

TIERS = ("base", "plus", "apex")
CREDITS = {"base": 1, "plus": 3, "apex": 10}
# Every turn sends the whole conversation again, so the cost of a review grows with the
# square of its turns. The model can ask for several tools in one turn.
DEFAULT_TURNS = {"base": 10, "plus": 16, "apex": 24}
DEFAULT_CONCURRENT_REVIEWS = 4


@dataclass
class GatewayConfig:
	url: str
	api_key: str
	dialect: str
	timeout: int
	models: dict[str, str] = field(default_factory=dict)
	fast_model: str = ""
	embedding_model: str = ""
	embedding_dimensions: int = 1024

	@property
	def enabled(self) -> bool:
		return bool(self.url and self.models.get("base"))

	def model_for(self, tier: str) -> str:
		"""Model for a tier. Falls back to the next lower tier that has a model."""
		order = TIERS[: TIERS.index(tier) + 1] if tier in TIERS else ("base",)
		for name in reversed(order):
			if self.models.get(name):
				return self.models[name]
		return self.models.get("base", "")


def settings() -> "GuardSettings":
	return cast("GuardSettings", frappe.get_cached_doc("Guard Settings"))


def gateway() -> GatewayConfig:
	doc = settings()
	api_key = doc.get_password("gateway_api_key", raise_exception=False)
	return GatewayConfig(
		url=(doc.gateway_url or "").rstrip("/"),
		api_key=str(api_key or ""),
		dialect=doc.gateway_dialect or "openai",
		timeout=cint(doc.request_timeout) or 300,
		models={"base": doc.base_model or "", "plus": doc.plus_model or "", "apex": doc.apex_model or ""},
		fast_model=doc.fast_model or doc.base_model or "",
		embedding_model=doc.embedding_model or "",
		embedding_dimensions=cint(doc.embedding_dimensions) or 1024,
	)


@dataclass
class Prices:
	"""USD for one million tokens, from Guard Settings. Only for cost estimates: the gateway bills."""

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


def prices() -> Prices:
	doc = settings()
	return Prices(flt(doc.input_price), flt(doc.cached_input_price), flt(doc.output_price))


def max_turns(tier: str) -> int:
	return cint(_value(f"max_turns_{tier}")) or DEFAULT_TURNS.get(tier, 15)


def max_concurrent_reviews() -> int:
	"""Reviews that may run at once. 0 means no limit."""
	value = _value("max_concurrent_reviews")
	return DEFAULT_CONCURRENT_REVIEWS if value in (None, "") else max(cint(value), 0)


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
