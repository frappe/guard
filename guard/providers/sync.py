"""Keep Guard Teams and Code Repositories in step with GitHub."""

import frappe

from guard import settings as guard_settings
from guard.providers import adapter_for_provider
from guard.providers.base import ProviderAdapter, ProviderError, RepositoryInfo
from guard.utils import db
from guard.utils.redact import redact

# The listing returns the whole installation, 100 repositories per call. Above this batch size it
# costs fewer calls than one call for each repository, except on very large installations.
BULK_LOOKUP = 50


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


def discover_installations(provider: str) -> None:
	"""App mode: create a team for each installation, so a sync works even when
	the installation webhook did not arrive."""
	if db.value("Code Provider", provider, "auth_mode") != "App":
		return
	adapter = adapter_for_provider(provider)
	for item in adapter.paginate_as_app("/app/installations"):
		account = (item.get("account") or {}).get("login") or ""
		if account:
			ensure_team(provider, account, str(item["id"]))


def disable_repositories(provider: str, namespace: str, keep: list[str] | None = None) -> None:
	"""Turn reviews off for the repositories of a namespace that Guard can no longer reach,
	except `keep`. Match by name, not by team: rows made before their team have no team."""
	# GitHub names ignore case, like the database. `_` is a LIKE wildcard, so check the prefix again.
	prefix = f"{namespace}/".casefold()
	kept = {name.casefold() for name in keep or ()}
	filters = {"code_provider": provider, "full_name": ["like", f"{namespace}/%"], "reviews_enabled": 1}
	rows = frappe.get_all("Code Repository", filters=filters, fields=["name", "full_name"])
	_disable(
		[
			row.name
			for row in rows
			if row.full_name.casefold().startswith(prefix) and row.full_name.casefold() not in kept
		]
	)


def disable_removed(provider: str, full_names: list[str]) -> None:
	"""Turn reviews off for repositories removed from an installation."""
	if not full_names:
		return
	filters = {"code_provider": provider, "full_name": ["in", full_names], "reviews_enabled": 1}
	_disable(frappe.get_all("Code Repository", filters=filters, pluck="name"))


def _disable(repositories: list[str]) -> None:
	# Keep the rows, so that their reviews, findings and indexes stay. An admin turns reviews on
	# again when the repository comes back.
	for name in repositories:
		db.set_values("Code Repository", name, {"reviews_enabled": 0})


def sync_team(team: str) -> int:
	"""Read every repository of a team from GitHub."""
	row = db.values("Guard Team", team, ("code_provider", "namespace")) or {}
	provider = row["code_provider"]
	adapter = adapter_for_provider(provider, team)
	repositories = adapter.list_repositories(row["namespace"])
	for info in repositories:
		ensure_repository(provider, info, team)
	# Only an App installation lists every repository Guard can reach. A token lists the
	# public repositories of a user, so a private repository would look removed.
	if db.value("Code Provider", provider, "auth_mode") == "App":
		disable_repositories(provider, row["namespace"], keep=[info.full_name for info in repositories])
	return len(repositories)


def on_installation(
	provider: str,
	action: str,
	namespace: str,
	installation_id: str | None,
	repositories: list[RepositoryInfo],
	removed: list[str] | None = None,
) -> None:
	if action == "deleted":
		disable_repositories(provider, namespace)
		return
	if action == "suspend":
		# A suspension is temporary. Keep `reviews_enabled`, so that unsuspend needs no repair.
		return
	disable_removed(provider, removed or [])
	team = ensure_team(provider, namespace, installation_id)
	adapter = adapter_for_provider(provider, team)
	for info in _with_details(adapter, namespace, repositories):
		ensure_repository(provider, info, team)


def _with_details(
	adapter: ProviderAdapter, namespace: str, repositories: list[RepositoryInfo]
) -> list[RepositoryInfo]:
	"""Fill in the clone URLs that installation events leave out. Skip a repository that GitHub
	does not show (404), for example a security advisory fork, so that the others still sync."""
	listed: dict[str, RepositoryInfo] = {}
	if sum(1 for info in repositories if not info.clone_url) > BULK_LOOKUP:
		try:
			listed = {info.full_name.casefold(): info for info in adapter.list_repositories(namespace)}
		except ProviderError as error:
			# The listing only saves calls. Read each repository instead.
			frappe.log_error(title="Guard sync", message=redact(str(error)))
	result = []
	for info in repositories:
		if info.clone_url:
			result.append(info)
			continue
		full = listed.get(info.full_name.casefold())
		if not full:
			try:
				full = adapter.get_repository(info.full_name)
			except ProviderError as error:
				# Other errors, such as a rate limit or an outage, fail the delivery, so they stay visible.
				if error.status != 404:
					raise
				frappe.log_error(title="Guard sync", message=redact(str(error)))
				continue
		result.append(full)
	return result
