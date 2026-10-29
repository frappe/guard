"""Code Repositories follow the repositories of a GitHub App installation."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from guard.providers import sync
from guard.providers.base import RepositoryInfo
from guard.utils import db
from guard.webhooks import github, handler

PROVIDER = "Sync Provider"
OTHER_PROVIDER = "Other Sync Provider"


def make_provider(name: str) -> None:
	if not frappe.db.exists("Code Provider", name):
		frappe.get_doc({"doctype": "Code Provider", "title": name, "provider_type": "GitHub"}).insert(
			ignore_permissions=True
		)
	frappe.db.set_value("Code Provider", name, "auth_mode", "App")


def make_repository(full_name: str, provider: str = PROVIDER) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Code Repository",
			"full_name": full_name,
			"code_provider": provider,
			"reviews_enabled": 1,
		}
	).insert(ignore_permissions=True)
	return str(doc.name)


def enabled(repository: str) -> bool:
	return bool(db.value("Code Repository", repository, "reviews_enabled"))


def info(full_name: str) -> RepositoryInfo:
	return RepositoryInfo(full_name, "1", "main", f"https://github.test/{full_name}.git", "")


class TestInstallationSync(IntegrationTestCase):
	def setUp(self):
		make_provider(PROVIDER)
		make_provider(OTHER_PROVIDER)
		self.kept = make_repository("acme/kept")
		self.gone = make_repository("acme/gone")
		self.other = make_repository("other/app")
		self.other_provider = make_repository("acme/kept", OTHER_PROVIDER)

	def installation(self, event: str, action: str, login: str = "acme", **payload) -> None:
		payload = {"action": action, "installation": {"id": 9, "account": {"login": login}}, **payload}
		for parsed in github.parse(event, payload):
			handler.dispatch(PROVIDER, parsed)

	def test_removed_repository_turns_reviews_off_and_keeps_the_row(self):
		self.installation(
			"installation_repositories",
			"removed",
			repository_selection="selected",
			repositories_added=[],
			repositories_removed=[{"id": 2, "full_name": "acme/gone", "private": True}],
		)
		self.assertTrue(frappe.db.exists("Code Repository", self.gone))
		self.assertFalse(enabled(self.gone))
		self.assertTrue(enabled(self.kept))
		self.assertTrue(db.value("Guard Team", {"code_provider": PROVIDER, "namespace": "acme"}, "name"))

	def test_deleted_installation_turns_reviews_off_for_its_namespace(self):
		self.installation("installation", "deleted")
		self.assertFalse(enabled(self.kept))
		self.assertFalse(enabled(self.gone))
		self.assertTrue(enabled(self.other))
		self.assertTrue(enabled(self.other_provider))

	def test_deleted_installation_matches_the_namespace_exactly(self):
		# `_` is a LIKE wildcard: `ac_e/%` also matches `acme/...`.
		near = make_repository("ac_e/app")
		self.installation("installation", "deleted", login="ac_e")
		self.assertFalse(enabled(near))
		self.assertTrue(enabled(self.kept))

	def test_suspended_installation_keeps_reviews_on(self):
		self.installation("installation", "suspend")
		self.assertTrue(enabled(self.kept))
		self.assertTrue(enabled(self.gone))

	def test_sync_turns_reviews_off_for_repositories_the_app_no_longer_reaches(self):
		team = sync.ensure_team(PROVIDER, "acme", "9")
		adapter = MagicMock()
		adapter.list_repositories.return_value = [info("acme/kept")]
		with patch.object(sync, "adapter_for_provider", return_value=adapter):
			sync.sync_team(team)
		self.assertTrue(enabled(self.kept))
		self.assertFalse(enabled(self.gone))
		self.assertTrue(enabled(self.other))
		self.assertTrue(enabled(self.other_provider))

	def test_sync_ignores_case_in_repository_names(self):
		team = sync.ensure_team(PROVIDER, "acme", "9")
		adapter = MagicMock()
		adapter.list_repositories.return_value = [info("Acme/Kept"), info("acme/gone")]
		with patch.object(sync, "adapter_for_provider", return_value=adapter):
			sync.sync_team(team)
		self.assertTrue(enabled(self.kept))

	def test_token_sync_keeps_repositories_it_does_not_list(self):
		# A token lists only the public repositories of a user.
		frappe.db.set_value("Code Provider", PROVIDER, "auth_mode", "Token")
		team = sync.ensure_team(PROVIDER, "acme")
		adapter = MagicMock()
		adapter.list_repositories.return_value = [info("acme/kept")]
		with patch.object(sync, "adapter_for_provider", return_value=adapter):
			sync.sync_team(team)
		self.assertTrue(enabled(self.gone))
