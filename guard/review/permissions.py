"""Who Guard answers: authoring, tagging, chat and teaching."""

import frappe

from guard import settings as guard_settings
from guard.providers import adapter_for_repository
from guard.providers.base import ProviderError
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
	return is_member(repository, login)


def who(kind: str, repository: str) -> str:
	"""The people who pass the setting for this kind of interaction, for a message to a person."""
	mode = getattr(guard_settings.settings(), KINDS[kind], None) or "Everyone"
	if mode == "Everyone":
		return "everyone"
	if mode == "Guard users":
		return "people with a Guard account"
	namespace = _namespace(repository)
	return f"members of the {namespace} organization" if namespace else "organization members"


def is_member(repository: str, login: str) -> bool:
	namespace = _namespace(repository)
	key = f"guard:member:{namespace}:{login}"
	cached = cache.get(key)
	if cached is not None:
		return bool(int(cached))
	adapter = adapter_for_repository(repository)
	# Not cached when the provider cannot tell: an error is not an answer.
	member = bool(adapter and adapter.is_member(namespace, login))
	cache.set(key, int(member), 3600)
	return member


def _namespace(repository: str) -> str:
	return str(db.value("Code Repository", repository, "full_name") or "").split("/")[0]


def user_for_login(login: str) -> str | None:
	name = db.value("Guard User", {"github_login": login}, "user")
	return str(name) if name else None
