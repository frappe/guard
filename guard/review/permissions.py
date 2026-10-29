"""Who Guard answers: authoring, tagging, chat and teaching."""

from collections.abc import Callable

import frappe

from guard import settings as guard_settings
from guard.providers import adapter_for_repository
from guard.providers.base import ProviderAdapter, ProviderError
from guard.utils import cache, db

KINDS = {
	"author": "trigger_by_authoring",
	"tag": "trigger_by_tagging",
	"chat": "chat_permission",
	"teach": "teach_permission",
}


def allowed(kind: str, login: str, repository: str) -> bool:
	"""True when the login passes the setting for this kind of interaction. False when the provider
	cannot tell."""
	try:
		return passes(kind, login, repository)
	except ProviderError:
		return False


def passes(kind: str, login: str, repository: str) -> bool:
	"""As `allowed`, but raise ProviderError when the provider cannot tell if the login is a member."""
	mode = getattr(guard_settings.settings(), KINDS[kind], None) or "Everyone"
	if mode == "Everyone":
		return True
	if mode == "Guard users":
		return bool(login) and bool(frappe.db.exists("Guard User", {"github_login": login}))
	return member_or_writer(repository, login)


def member_or_writer(repository: str, login: str) -> bool:
	"""A member of the namespace, or a person with write access to the repository: outside
	maintainers are often collaborators and not members. Raise ProviderError only when neither
	answer is known."""
	unknown: ProviderError | None = None
	for check in (is_member, can_write):
		try:
			if check(repository, login):
				return True
		except ProviderError as error:
			unknown = error
	if unknown:
		raise unknown
	return False


def who(kind: str, repository: str) -> str:
	"""The people who pass the setting for this kind of interaction, for a message to a person."""
	mode = getattr(guard_settings.settings(), KINDS[kind], None) or "Everyone"
	if mode == "Everyone":
		return "everyone"
	if mode == "Guard users":
		return "people with a Guard account"
	full_name = _full_name(repository)
	if not full_name:
		return "organization members and people with write access"
	namespace = full_name.split("/")[0]
	return f"members of the {namespace} organization and people with write access to {full_name}"


def is_member(repository: str, login: str) -> bool:
	namespace = _full_name(repository).split("/")[0]
	return _cached(
		f"guard:member:{namespace}:{login}",
		repository,
		lambda adapter: adapter.is_member(namespace, login),
	)


def can_write(repository: str, login: str) -> bool:
	full_name = _full_name(repository)
	return _cached(
		f"guard:writer:{full_name}:{login}",
		repository,
		lambda adapter: adapter.can_write(full_name, login),
	)


def _cached(key: str, repository: str, ask: Callable[[ProviderAdapter], bool]) -> bool:
	"""The answer of the provider, kept for one hour. Raise ProviderError when no provider answers:
	no adapter, or an error, is not an answer, so it is not cached."""
	cached = cache.get(key)
	if cached is not None:
		return bool(int(cached))
	adapter = adapter_for_repository(repository)
	if not adapter:
		raise ProviderError("The repository has no code provider")
	answer = bool(ask(adapter))
	cache.set(key, int(answer), 3600)
	return answer


def _full_name(repository: str) -> str:
	return str(db.value("Code Repository", repository, "full_name") or "")


def user_for_login(login: str) -> str | None:
	name = db.value("Guard User", {"github_login": login}, "user")
	return str(name) if name else None
