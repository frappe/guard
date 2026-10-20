"""Build the git store of a Code Repository with its provider credentials."""

from typing import TYPE_CHECKING, cast

import frappe

from guard.git.store import GitStore

if TYPE_CHECKING:
	from guard.guard.doctype.code_repository.code_repository import CodeRepository


def store_for(repository: str) -> GitStore:
	doc = cast("CodeRepository", frappe.get_cached_doc("Code Repository", repository))
	header = None
	url = doc.clone_url or ""
	if doc.code_provider and not _is_local(url):
		from guard.providers import adapter_for_repository

		adapter = adapter_for_repository(repository)
		header = adapter.git_auth_header() if adapter else None
	return GitStore(str(doc.name), url, header)


def _is_local(url: str) -> bool:
	return url.startswith(("/", "file://"))
