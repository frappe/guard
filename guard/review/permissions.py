"""Who Guard answers: authoring, tagging, chat and teaching."""

import frappe

from guard import settings as guard_settings
from guard.providers import adapter_for_repository
from guard.utils import cache, db

KINDS = {
	"author": "trigger_by_authoring",
	"tag": "trigger_by_tagging",
	"chat": "chat_permission",
	"teach": "teach_permission",
}


def allowed(kind: str, login: str, repository: str) -> bool:
	"""True when the login passes the setting for this kind of interaction."""
	mode = getattr(guard_settings.settings(), KINDS[kind], None) or "Everyone"
	if mode == "Everyone":
		return True
	if mode == "Guard users":
		return bool(login) and bool(frappe.db.exists("Guard User", {"github_login": login}))
	return is_member(repository, login)


def is_member(repository: str, login: str) -> bool:
	namespace = str(db.value("Code Repository", repository, "full_name") or "").split("/")[0]
	key = f"guard:member:{namespace}:{login}"
	cached = cache.get(key)
	if cached is not None:
		return bool(int(cached))
	adapter = adapter_for_repository(repository)
	member = bool(adapter and adapter.is_member(namespace, login))
	cache.set(key, int(member), 3600)
	return member


def user_for_login(login: str) -> str | None:
	name = db.value("Guard User", {"github_login": login}, "user")
	return str(name) if name else None
