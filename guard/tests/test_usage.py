import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils import add_days, now_datetime, today

from guard.guard.report.guard_usage import guard_usage
from guard.settings import Prices
from guard.tests.utils import TempRepo, make_repository

PRICES = {"input_price": 0.30, "cached_input_price": 0.03, "output_price": 1.20}


class TestPrices(UnitTestCase):
	def test_cached_tokens_are_charged_at_the_cached_price_only(self):
		prices = Prices(input=0.30, cached_input=0.03, output=1.20)
		# 1M prompt tokens, of which 750k from the cache, and 100k output tokens.
		self.assertAlmostEqual(prices.cost(1_000_000, 750_000, 100_000), 0.075 + 0.0225 + 0.12)
		self.assertFalse(Prices().known)

	def test_compact_counts(self):
		self.assertEqual(
			[guard_usage.compact(n) for n in (950, 41_567, 2_728_340, 3_100_000_000)],
			["950", "41.6K", "2.73M", "3.1B"],
		)


class TestGuardUsage(IntegrationTestCase):
	def setUp(self):
		self.repo = TempRepo({"README.md": "usage\n"})
		self.addCleanup(self.repo.cleanup)
		self.repository = make_repository(self.repo, "acme/usage")
		self.other = make_repository(self.repo, "acme/other")
		for name in PRICES:
			self.addCleanup(self.set_price, name, 0)

	def set_price(self, name: str, value: float) -> None:
		frappe.db.set_single_value("Guard Settings", name, str(value))
		frappe.clear_document_cache("Guard Settings", "Guard Settings")

	def add(self, tier: str, status: str, tokens: tuple[int, int, int], days_ago: int = 0, credits: int = 0):
		review = frappe.get_doc(
			{
				"doctype": "Code Review",
				"repository": self.repository if tier != "apex" else self.other,
				"source": "cli",
				"status": status,
				"trigger": "cli",
				"tier": tier,
				"credits": credits,
				"input_tokens": tokens[0],
				"cached_tokens": tokens[1],
				"output_tokens": tokens[2],
				"agent_turns": 3,
			}
		).insert(ignore_permissions=True)
		# The class rolls back only at its end: remove the rows so the next test does not count them.
		self.addCleanup(frappe.delete_doc, "Code Review", review.name, force=True, ignore_permissions=True)
		created = add_days(now_datetime(), -days_ago)
		frappe.db.set_value("Code Review", str(review.name), "creation", created, update_modified=False)

	def report(self, **filters) -> tuple:
		return guard_usage.execute({"from_date": add_days(today(), -6), "to_date": today(), **filters})

	def seed(self) -> None:
		self.add("plus", "Completed", (1_000_000, 750_000, 100_000), credits=3)
		# A failed review used tokens but costs no credits.
		self.add("plus", "Failed", (200_000, 0, 10_000), credits=3)
		self.add("apex", "Completed", (4_000_000, 3_000_000, 200_000), days_ago=2, credits=10)
		self.add("base", "Completed", (50_000, 0, 5_000), days_ago=30, credits=1)  # outside the period

	def test_rows_by_tier_count_tokens_of_every_review_and_credits_of_completed_ones(self):
		self.seed()
		for name, value in PRICES.items():
			self.set_price(name, value)
		columns, rows, message, chart, cards = self.report(group_by="Tier")
		rows = {row["bucket"]: row for row in rows if row["bucket"] in ("plus", "apex", "base")}
		self.assertNotIn("base", rows)
		plus = rows["plus"]
		self.assertEqual((plus["reviews"], plus["completed"], plus["credits"]), (2, 1, 3))
		self.assertEqual(plus["input_tokens"], 1_200_000)
		self.assertEqual(plus["cache_share"], 62.5)
		# 450k uncached input, 750k cached input and 110k output tokens.
		self.assertAlmostEqual(plus["cost"], (450_000 * 0.30 + 750_000 * 0.03 + 110_000 * 1.20) / 1e6)
		self.assertAlmostEqual(plus["cost_per_review"], plus["cost"])  # one completed review
		self.assertIn("cost", [c["fieldname"] for c in columns])
		self.assertIsNone(message)
		self.assertEqual(cards[-1]["label"], "Est. Cost (USD)")
		self.assertEqual(len(cards), 5)
		self.assertEqual(chart["data"]["datasets"][0]["name"], "Est. Cost (USD)")

	def test_rows_by_repository_and_day(self):
		self.seed()
		_, rows, *_ = self.report(group_by="Repository", repository=self.repository)
		self.assertEqual([row["bucket"] for row in rows], ["acme/usage"])
		self.assertEqual(rows[0]["reviews"], 2)
		_, rows, *_ = self.report(group_by="Day", tier="apex")
		self.assertEqual([str(row["bucket"]) for row in rows], [str(add_days(today(), -2))])

	def test_without_prices_there_is_no_cost_and_a_hint(self):
		self.seed()
		columns, _, message, chart, cards = self.report(repository=self.repository)
		self.assertNotIn("cost", [c["fieldname"] for c in columns])
		self.assertIn("Guard Settings", message)
		self.assertEqual([d["name"] for d in chart["data"]["datasets"]], ["Uncached Input", "Cached Input"])
		self.assertNotIn("Est. Cost (USD)", [card["label"] for card in cards])
