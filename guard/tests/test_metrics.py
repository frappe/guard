from datetime import datetime, timedelta

from frappe.tests import UnitTestCase

from guard.review import metrics
from guard.review.metrics import Review
from guard.settings import PriceList, Prices

NOW = datetime(2026, 10, 26, 15, 30)
PRICES = PriceList({"base": Prices(input=2.0, cached_input=0.1, output=10.0)})


def review(minutes_ago: int, **values) -> Review:
	ended = NOW - timedelta(minutes=minutes_ago)
	defaults = {
		"ended": ended,
		"created": ended - timedelta(minutes=12),
		"started": ended - timedelta(minutes=10),
		"status": "Completed",
		"tier": "base",
		"seconds": 600.0,
		"turns": 10,
		"rounds": 10,
		"input_tokens": 1_000_000,
		"output_tokens": 100_000,
		"cached_tokens": 500_000,
		"findings": 2,
		"posted": 1,
		"attempt": 0,
		"stages": {"prepare": 20.0, "review": 300.0, "publish": 10.0},
	}
	return Review(**{**defaults, **values})


class TestMetrics(UnitTestCase):
	def test_stats_of_a_period(self):
		reviews = [
			review(10),
			review(20, tier="apex", seconds=1200.0, turns=30, rounds=12, attempt=1, stages={}),
			review(30, status="Failed", turns=4, rounds=4, seconds=60.0),
			review(40, status="Cancelled", input_tokens=0, output_tokens=0, cached_tokens=0),
		]
		stats = metrics._stats(reviews, PRICES)
		self.assertEqual((stats["completed"], stats["failed"], stats["cancelled"]), (2, 1, 1))
		# Time, turns and rounds come from the completed reviews only.
		self.assertEqual((stats["duration_p50"], stats["duration_p90"]), (10.0, 20.0))
		self.assertEqual((stats["turns"], stats["rounds"]), (20.0, 11.0))
		self.assertEqual(stats["wait_p50"], 2.0)
		# 40 turns over 5 minutes of the review stage and 20 minutes of a review with no stage times.
		self.assertEqual(stats["turns_per_minute"], 1.6)
		# Tokens and cost count the failed review too: it spent them.
		self.assertEqual(stats["input_tokens"], 3_000_000)
		self.assertEqual(stats["cache_rate"], 0.5)
		self.assertEqual(stats["cost"], round(3 * (0.5 * 2 + 0.5 * 0.1 + 0.1 * 10), 2))
		self.assertEqual(stats["retried"], 0.5)

	def test_no_reviews_give_no_numbers(self):
		stats = metrics._stats([], PRICES)
		self.assertEqual(stats["completed"], 0)
		self.assertIsNone(stats["duration_p50"])
		self.assertIsNone(stats["turns_per_minute"])
		self.assertIsNone(stats["cost_per_review"])
		self.assertIsNone(metrics._stats([review(1)], PriceList())["cost"], "no prices, no cost")

	def test_each_review_costs_the_prices_of_its_tier(self):
		prices = PriceList({**PRICES.tiers, "apex": Prices(input=10.0, cached_input=0.25, output=50.0)})
		stats = metrics._stats([review(1), review(2, tier="apex"), review(3, tier="plus")], prices)
		base = 0.5 * 2 + 0.5 * 0.1 + 0.1 * 10  # plus has no prices: it costs the base prices
		self.assertEqual(stats["cost"], round(2 * base + 0.5 * 10 + 0.5 * 0.25 + 0.1 * 50, 2))

	def test_series_has_a_bucket_for_each_bar_of_the_period(self):
		reviews = [review(5), review(10), review(90), review(60 * 30)]
		hour = metrics._series(reviews, NOW, 1, PRICES)
		self.assertEqual(len(hour), 12)  # 5 minutes a bar
		self.assertEqual([h["label"] for h in hour[-3:]], ["15:20", "15:25", "15:30"])
		self.assertEqual([h["completed"] for h in hour[-3:]], [1, 1, 0])
		six = metrics._series(reviews, NOW, 6, PRICES)
		self.assertEqual((len(six), six[-1]["label"], sum(h["completed"] for h in six)), (24, "15:30", 3))
		hours = metrics._series(reviews, NOW, 24, PRICES)
		self.assertEqual(len(hours), 24)
		self.assertEqual(hours[-1]["label"], "15:00")
		self.assertEqual([h["completed"] for h in hours[-2:]], [1, 2])
		days = metrics._series(reviews, NOW, 7 * 24, PRICES)
		self.assertEqual([d["label"] for d in days[-2:]], ["25 Oct", "26 Oct"])
		self.assertEqual([d["completed"] for d in days[-2:]], [1, 3])

	def test_stages_follow_the_pipeline(self):
		stages = metrics._stages([review(1), review(2, stages={}), review(3, stages={"review": 100.0})])
		self.assertEqual([s["stage"] for s in stages], list(metrics.STAGES))
		by_stage = {s["stage"]: s["seconds"] for s in stages}
		self.assertEqual((by_stage["review"], by_stage["prepare"], by_stage["verify"]), (200.0, 10.0, 0))

	def test_percentile(self):
		self.assertEqual(metrics._percentile([1.0, 2.0, 3.0, 4.0], 50), 2.0)
		self.assertEqual(metrics._percentile([1.0, 2.0, 3.0, 4.0], 90), 4.0)
		self.assertIsNone(metrics._percentile([], 50))
