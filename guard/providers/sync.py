"""Keep Guard Teams and Code Repositories in step with GitHub."""

import frappe

from guard import settings as guard_settings
from guard.providers import adapter_for_provider
from guard.providers.base import RepositoryInfo
from guard.utils import db


def find_repository(provider: str, full_name: str) -> str | None:
	name = db.value("Code Repository", {"code_provider": provider, "full_name": full_name}, "name")
	return str(name) if name else None


def ensure_team(provider: str, namespace: str, installation_id: str | None = None) -> str:
	name = db.value("Guard Team", {"code_provider": provider, "namespace": namespace}, "name")
	if name:
		if installation_id and db.value("Guard Team", name, "installation_id") != installation_id:
			frappe.db.set_value("Guard Team", name, "installation_id", installation_id)
		return str(name)
	doc = frappe.get_doc(
		{
			"doctype": "Guard Team",
			"team_name": namespace,
			"code_provider": provider,
			"namespace": namespace,
			"installation_id": installation_id,
		}
	).insert(ignore_permissions=True)
	return str(doc.name)


def ensure_repository(provider: str, info: RepositoryInfo, team: str | None) -> str:
	"""Create or update a Code Repository. New repositories follow auto-enable."""
	name = find_repository(provider, info.full_name)
	values = {
		"provider_repo_id": info.provider_repo_id,
		"default_branch": info.default_branch or "main",
		"clone_url": info.clone_url,
		"web_url": info.web_url,
		"is_private": int(info.is_private),
	}
	if name:
		db.set_values("Code Repository", name, {k: v for k, v in values.items() if v not in ("", None)})
		return name
	doc = frappe.get_doc(
		{
			"doctype": "Code Repository",
			"full_name": info.full_name,
			"code_provider": provider,
			"team": team,
			"reviews_enabled": int(guard_settings.flag("auto_enable_new_repositories")),
			**values,
		}
	).insert(ignore_permissions=True)
	if doc.get("reviews_enabled"):
		from guard.indexing.jobs import enqueue_index

		enqueue_index(str(doc.name))
	return str(doc.name)


def resolve_repository(
	provider: str, info: RepositoryInfo | None, namespace: str, installation_id: str | None = None
) -> str | None:
	"""The Code Repository of a webhook event, created on first sight."""
	if not info or not info.full_name:
		return None
	name = find_repository(provider, info.full_name)
	if name:
		return name
	team = ensure_team(provider, namespace or info.full_name.split("/")[0], installation_id)
	if not info.clone_url:
		info = adapter_for_provider(provider, team).get_repository(info.full_name)
	return ensure_repository(provider, info, team)


def discover_installations(provider: str, team: str, refresh: bool = False) -> None:
	"""App mode: create a team for each installation, so a sync works even when
	the installation webhook did not arrive."""
	if db.value("Code Provider", provider, "auth_mode") != "App":
		return
	adapter = adapter_for_provider(provider)
	for item in adapter.paginate_as_app("/app/installations"):
		account = (item.get("account") or {}).get("login") or ""
		if account:
			ensure_team(provider, account, str(item["id"]))


def sync_team(team: str) -> int:
	"""Read every repository of a team from GitHub."""
	row = db.values("Guard Team", team, ("code_provider", "namespace")) or {}
	adapter = adapter_for_provider(row["code_provider"], team)
	repositories = adapter.list_repositories(row["namespace"])
	for info in repositories:
		ensure_repository(row["code_provider"], info, team)
	return len(repositories)


def on_installation(
	provider: str,
	action: str,
	namespace: str,
	installation_id: str | None,
	repositories: list[RepositoryInfo],
) -> None:
	if action in ("deleted", "suspend"):
		return
	team = ensure_team(provider, namespace, installation_id)
	adapter = adapter_for_provider(provider, team)
	for info in repositories:
		full = adapter.get_repository(info.full_name) if not info.clone_url else info
		ensure_repository(provider, full, team)
