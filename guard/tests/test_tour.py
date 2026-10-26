from unittest.mock import patch

from frappe.tests import IntegrationTestCase

from guard.conversation import tour
from guard.git.diff import parse_diff
from guard.llm.client import ChatRequest, use_responder
from guard.llm.types import ChatResult, LLMError, Usage


def diff(files: int, hunks: int, width: int = 80) -> str:
	"""A diff of `files` files with `hunks` one-line hunks each."""
	parts = []
	for f in range(files):
		parts.append(f"diff --git a/f{f}.po b/f{f}.po\n--- a/f{f}.po\n+++ b/f{f}.po\n")
		for h in range(hunks):
			line = h * 10 + 1
			parts.append(f"@@ -{line},1 +{line},1 @@\n-old {'x' * width}\n+new {'y' * width}\n")
	return "".join(parts)


class TestTour(IntegrationTestCase):
	def setUp(self):
		self.requests: list[ChatRequest] = []
		online = patch("guard.llm.client.LLMClient.online", new=True)
		online.start()
		self.addCleanup(online.stop)

	def answer(self, text: str = "[]"):
		def respond(request: ChatRequest) -> ChatResult:
			self.requests.append(request)
			return ChatResult(text, [], Usage(10, 5))

		return use_responder(respond)

	def test_listing_of_many_hunks_stays_in_budget(self):
		files = parse_diff(diff(10, 30, width=500))
		with self.answer():
			tour.tour_steps(files)
		prompt = self.requests[0].messages[-1].content
		self.assertLess(len(prompt), tour.LISTING_CHARS + 1000)
		# Each hunk shows less code, but every hunk is still listed.
		for hunk in (h for f in files for h in f.hunks):
			self.assertIn(f"\n{hunk.id} ", f"\n{prompt}")

	def test_pull_request_with_too_many_hunks_goes_by_file_without_the_model(self):
		files = parse_diff(diff(3, tour.MAX_HUNKS // 2))
		with self.answer():
			steps = tour.tour_steps(files)
		self.assertFalse(self.requests)
		self.assertEqual([s["title"] for s in steps], ["f0.po", "f1.po", "f2.po"])

	def test_gateway_error_falls_back_to_steps_by_file(self):
		files = parse_diff(diff(2, 2))

		def refuse(request: ChatRequest) -> ChatResult:
			raise LLMError('Gateway answered 413: {"error":{"message":"request body too large"}}')

		with use_responder(refuse):
			steps = tour.tour_steps(files)
		self.assertEqual([s["title"] for s in steps], ["f0.po", "f1.po"])
		self.assertEqual(sum(len(s["hunks"]) for s in steps), 4)
