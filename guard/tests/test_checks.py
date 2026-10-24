from typing import Any
from unittest.mock import patch

from frappe.tests import UnitTestCase

from guard.providers.base import ProviderAdapter, ProviderError
from guard.providers.github import GitHubAdapter
from guard.tests.fakes import FakeDoc

APP = {"id": 7}
OTHER_APP = {"id": 99}


class FakeGitHub(GitHubAdapter):
	"""Answers the check-run calls from memory."""

	def __init__(self, runs: list[dict[str, Any]]):
		super().__init__(FakeDoc({"auth_mode": "App", "app_id": "7"}))
		self.runs = runs
		self.sent: list[tuple[str, str, dict[str, Any] | None]] = []

	def request(self, method: str, path: str, auth: bool = True, **kwargs: Any) -> Any:
		self.sent.append((method, path, kwargs.get("json")))
		if method == "GET":
			return {"check_runs": self.runs}
		if method == "POST":
			return {"id": 500}
		return {}

	def patched(self) -> list[str]:
		return [path.rsplit("/", 1)[-1] for method, path, _ in self.sent if method == "PATCH"]


def run(run_id: int, status: str = "in_progress", app: dict = APP, started: str = "2026-10-24T09:00:00Z"):
	return {"id": run_id, "status": status, "app": app, "started_at": started}


class TestCheckRuns(UnitTestCase):
	def test_close_open_checks_closes_only_guard_runs_in_progress(self):
		github = FakeGitHub([run(1), run(2, status="completed"), run(3, app=OTHER_APP), run(4)])
		self.assertEqual(github.close_open_checks("acme/app", "abc", "Stopped."), 2)
		self.assertEqual(sorted(github.patched()), ["1", "4"])
		payload = next(body for method, _, body in github.sent if method == "PATCH")
		self.assertEqual((payload or {})["conclusion"], "neutral")
		self.assertFalse([1 for method, _, _ in github.sent if method == "POST"])

	def test_a_lost_cache_reuses_the_run_in_progress(self):
		# A deploy clears the cache. Before, the next status made a second run, and the first one
		# said "Guard is reviewing" for ever.
		github = FakeGitHub([run(1, started="2026-10-24T08:00:00Z"), run(2, started="2026-10-24T09:00:00Z")])
		with (
			patch("guard.providers.github.cache.get", return_value=None),
			patch("guard.providers.github.cache.set"),
		):
			github.set_status("acme/app", "abc", "pending", "Guard is reviewing")
			self.assertEqual(github.patched(), ["2"])
			github.sent.clear()
			github.set_status("acme/app", "abc", "success", "5/5")
		self.assertEqual(sorted(github.patched()), ["1", "2"])
		self.assertFalse([1 for method, _, _ in github.sent if method == "POST"])

	def test_a_new_commit_gets_a_new_run(self):
		github = FakeGitHub([])
		with (
			patch("guard.providers.github.cache.get", return_value=None),
			patch("guard.providers.github.cache.set") as remember,
		):
			github.set_status("acme/app", "abc", "pending", "Guard is reviewing")
		self.assertEqual([method for method, _, _ in github.sent], ["GET", "POST"])
		remember.assert_called_once()


class TestInstallationToken(UnitTestCase):
	def adapter(self) -> GitHubAdapter:
		github = GitHubAdapter(FakeDoc({"auth_mode": "App", "app_id": "7"}, name="gh"), team="t")
		github._installation_token = lambda: "token"
		return github

	def test_a_refused_token_is_dropped_and_the_request_goes_again_once(self):
		refused = ProviderError("POST /x: 401 Bad credentials", 401)
		with (
			patch.object(ProviderAdapter, "request", side_effect=[refused, {"ok": 1}]) as sent,
			patch("guard.providers.github.db.value", return_value="42"),
			patch("guard.providers.github.cache.delete") as delete,
		):
			self.assertEqual(self.adapter().request("POST", "/x"), {"ok": 1})
		delete.assert_called_once_with("guard:github-token:gh:42")
		self.assertEqual(sent.call_count, 2)

	def test_other_errors_and_app_requests_go_once(self):
		for error, auth in ((ProviderError("404", 404), True), (ProviderError("401", 401), False)):
			with (
				patch.object(ProviderAdapter, "request", side_effect=[error]) as sent,
				self.assertRaises(ProviderError),
			):
				self.adapter().request("GET", "/x", auth=auth)
			self.assertEqual(sent.call_count, 1)
