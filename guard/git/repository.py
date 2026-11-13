"""Build the git store of a Code Repository with its provider credentials."""

from typing import TYPE_CHECKING, cast

import frappe

from guard.git.store import GitError, GitStore
from guard.utils import db

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


def default_branch(repository: str) -> str:
	return str(db.value("Code Repository", repository, "default_branch") or "")


def fetch_default(repository: str, store: GitStore) -> None:
	"""Fetch the default branch, for the config. Apart from the other branches: when the remote does
	not have it, the review still gets its base branch, and `default_head` says what is missing."""
	if branch := default_branch(repository):
		try:
			store.fetch_branches(branch)
		except GitError:
			pass


def default_head(repository: str, store: GitStore) -> str:
	"""The head of the default branch in the mirror. Raise GitError when the mirror does not have it.

	All config and rule files come from it. A pull request cannot change them, and a pull request to
	another branch, or a local change from an old base, gets the config that the team has now. No
	other commit is a safe fallback: the config of any other branch can come from a pull request.
	"""
	branch = default_branch(repository)
	ref = f"refs/heads/{branch}"
	if branch and not store.has_commit(ref):
		fetch_default(repository, store)
	try:
		return store.rev_parse(ref)
	except GitError as error:
		raise GitError(
			f"Guard reads the config from the default branch `{branch}` of {repository}, and the mirror "
			"does not have it. Check the default branch of the Code Repository."
		) from error


def _is_local(url: str) -> bool:
	return url.startswith(("/", "file://"))
