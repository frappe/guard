"""Code provider adapters. The review engine talks only to `ProviderAdapter`."""

from typing import TYPE_CHECKING

import frappe

from guard.utils import db

if TYPE_CHECKING:
	from guard.providers.base import ProviderAdapter

ADAPTERS = {
	"GitHub": "guard.providers.github.GitHubAdapter",
	"GitLab": "guard.providers.gitlab.GitLabAdapter",
	"Gitea": "guard.providers.gitea.GiteaAdapter",
	"Bitbucket": "guard.providers.bitbucket.BitbucketAdapter",
}


def adapter_for_provider(provider: str, team: str | None = None) -> "ProviderAdapter":
	doc = frappe.get_cached_doc("Code Provider", provider)
	cls = frappe.get_attr(ADAPTERS[str(doc.get("provider_type"))])
	return cls(doc, team)


def adapter_for_repository(repository: str) -> "ProviderAdapter | None":
	row = db.values("Code Repository", repository, ("code_provider", "team"))
	if not row or not row["code_provider"]:
		return None
	return adapter_for_provider(row["code_provider"], row["team"])
