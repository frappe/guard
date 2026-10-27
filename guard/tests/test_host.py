import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase, UnitTestCase
from frappe.utils.background_jobs import create_job_id, get_redis_conn
from rq.job import Job, JobStatus
from rq.queue import Queue

from guard.indexing import parser
from guard.review import host, jobs

redis = host._redis
redis_key = host._key


def clear(*names: str) -> None:
	for name in names:
		redis().delete(redis_key(name))


class TestHost(IntegrationTestCase):
	def setUp(self):
		# A queue that no worker of the bench listens on.
		self.queue = Queue(f"guard-test-host-{frappe.generate_hash(length=8)}", connection=get_redis_conn())
		self.addCleanup(self.queue.delete, delete_jobs=True)
		self.marks: list[str] = ["guard-test-host-free"]
		self.addCleanup(lambda: clear(*self.marks))

	def add(self, review: str, timeout: int = 1800, method: str = host.METHOD, site: str = "") -> Job:
		self.marks.append(host.alive_key(review, 0))
		return self.queue.enqueue_call(
			"frappe.utils.background_jobs.execute_job",
			kwargs={
				"site": site or frappe.local.site,
				"user": "Administrator",
				"method": method,
				"event": None,
				"job_name": method,
				"is_async": True,
				"kwargs": {"review": review, "attempt": 0, "tier": "", "moved": False},
			},
			timeout=timeout,
			job_id=create_job_id(jobs.job_id(review, 0)),
		)

	def test_a_host_takes_only_review_jobs_of_its_site_that_fit_in_its_time(self):
		self.add("CR-SITE", site="other.localhost")
		self.add("CR-METHOD", method="guard.indexing.jobs.run")
		self.add("CR-LONG", timeout=7200)
		wanted = self.add("CR-FITS")
		taken = host.Host(self.queue, 2, 3600).take()
		assert taken is not None
		self.assertEqual(taken[0].id, wanted.id)
		self.assertNotIn(wanted.id, self.queue.get_job_ids())
		self.assertEqual(len(self.queue.get_job_ids()), 3)
		# The id is free for a park or a retry, and the mark tells that the review runs.
		self.assertFalse(Job.exists(wanted.id, connection=self.queue.connection))
		self.assertTrue(jobs.has_job("CR-FITS", 0))
		self.assertFalse(jobs.has_job("CR-LONG", 1))
		self.assertIsNone(host.Host(self.queue, 2, 3600).take())

	def test_a_job_that_the_host_cannot_take_goes_back_to_the_queue(self):
		wanted = self.add("CR-BACK")
		with patch.object(Job, "delete", side_effect=ConnectionError("redis")):
			with self.assertRaises(ConnectionError):
				host.Host(self.queue, 2, 3600).take()
		# Else the review counts as enqueued for good, and no cron queues it again.
		self.assertEqual(self.queue.get_job_ids(), [wanted.id])
		self.assertFalse(host.is_hosted("CR-BACK", 0))

	def test_an_error_in_a_look_does_not_end_the_host(self):
		hosted = host.Host(self.queue, 2, 3600)
		with patch.object(hosted, "fill", side_effect=ValueError("bad job")):
			self.assertFalse(hosted.step())

	def test_a_host_runs_each_review_on_its_own_thread_and_connection(self):
		self.add("CR-A")
		self.add("CR-B", timeout=1500)
		seen: list[tuple[str, str, bool, dict]] = []
		main_db = frappe.local.db

		def run(cls, review, attempt=0, tier=None, moved=False, timeout=0):
			thread = threading.current_thread()
			seen.append((thread.name, frappe.local.site, frappe.local.db is main_db, {"timeout": timeout}))

		with (
			# The real `run_review`: it must take the timeout that the host passes.
			patch("guard.review.pipeline.ReviewPipeline.run", new=classmethod(run)),
			patch.object(host, "POLL", 0.05),
		):
			host.Host(self.queue, 2, 3600, "guard-test-host-free").serve()
		self.assertEqual(sorted(name for name, *_ in seen), ["guard-review-CR-A", "guard-review-CR-B"])
		self.assertTrue(all(site == frappe.local.site and not shared for _, site, shared, _ in seen))
		# Each review stops its agents in time for its own job, not for the host job.
		self.assertEqual(sorted(kwargs["timeout"] for *_, kwargs in seen), [1500, 1800])
		self.assertEqual(self.queue.get_job_ids(), [])
		self.assertFalse(jobs.has_job("CR-A", 0))
		self.assertFalse(redis().exists("guard-test-host-free"))

	def test_an_ended_thread_keeps_the_mark_of_a_newer_job(self):
		# A park keeps the attempt: a newer job of the review can run while the old thread ends.
		self.marks.append(host.alive_key("CR-OLD", 0))
		hosted = host.Host(self.queue, 2, 3600)
		old = host.Hosted("CR-OLD", 0, threading.Thread())
		hosted.running.append(old)
		hosted.mark()
		redis().set(redis_key(host.alive_key("CR-OLD", 0)), "newer", ex=60)
		hosted.mark()
		hosted.reap()
		self.assertEqual(hosted.running, [])
		self.assertEqual(redis().get(redis_key(host.alive_key("CR-OLD", 0))), b"newer")

	def test_a_host_with_nothing_to_take_ends(self):
		with patch.object(host, "POLL", 0.05):
			host.Host(self.queue, 2, 3600).serve()


class TestEnsureHost(UnitTestCase):
	def setUp(self):
		self.keys = [host.free_key("guard-base", index) for index in range(3)]
		self.addCleanup(clear, *self.keys)
		clear(*self.keys)

	def ensure(self, statuses: dict[int, JobStatus], started_ago: int = 0) -> list[int]:
		started = datetime.now(UTC) - timedelta(seconds=started_ago)
		deleted: list[str] = []

		def job_of(job_id: str):
			for index, status in statuses.items():
				if job_id == host.host_job_id("guard-base", index):
					return SimpleNamespace(
						get_status=lambda refresh=False, s=status: s,
						started_at=started,
						delete=lambda job_id=job_id: deleted.append(job_id),
					)
			return None

		# Each call stands for a new request.
		frappe.flags.pop("guard_hosts_ensured", None)
		with (
			patch("guard.review.host.get_job", side_effect=job_of),
			patch("guard.review.host.frappe.enqueue") as enqueue,
		):
			host.ensure_host("guard-base")
		frappe.flags.pop("guard_hosts_ensured", None)
		self.deleted = deleted
		return [call.kwargs["index"] for call in enqueue.call_args_list]

	def free(self, index: int, count: int) -> None:
		redis().set(redis_key(self.keys[index]), count, ex=60)

	def test_a_host_is_queued_when_none_can_take_the_review(self):
		self.assertEqual(self.ensure({}), [0])
		# A host that runs with every thread busy takes nothing more: queue the next one.
		self.free(0, 0)
		self.assertEqual(self.ensure({0: JobStatus.STARTED}), [1])
		self.assertEqual(self.ensure({0: JobStatus.FINISHED}), [0])

	def test_no_host_is_queued_when_one_can_take_the_review(self):
		self.assertEqual(self.ensure({0: JobStatus.QUEUED}), [])
		# Also when the queued host has a higher index than a free one.
		self.assertEqual(self.ensure({1: JobStatus.QUEUED}), [])

	def test_each_review_takes_one_free_thread(self):
		self.free(0, 1)
		self.assertEqual(self.ensure({0: JobStatus.STARTED}), [])
		self.assertEqual(self.ensure({0: JobStatus.STARTED}), [1])

	def test_the_index_of_a_dead_host_is_used_again(self):
		# A new host has not told its free threads yet: it is not dead.
		self.assertEqual(self.ensure({0: JobStatus.STARTED}, started_ago=1), [1])
		self.assertEqual(self.deleted, [])
		self.assertEqual(self.ensure({0: JobStatus.STARTED}, started_ago=host.FREE_TTL * 3), [0])
		self.assertEqual(self.deleted, [host.host_job_id("guard-base", 0)])

	def test_a_request_looks_once_for_each_queue(self):
		frappe.flags.pop("guard_hosts_ensured", None)
		with (
			patch("guard.review.host.get_job", return_value=None),
			patch("guard.review.host.frappe.enqueue") as enqueue,
		):
			host.ensure_host("guard-base")
			host.ensure_host("guard-base")
		frappe.flags.pop("guard_hosts_ensured", None)
		enqueue.assert_called_once()

	def test_only_review_queues_get_hosts_and_an_error_does_not_fail_the_caller(self):
		with (
			patch("guard.settings.reviews_per_worker", return_value=4),
			patch("guard.review.jobs.ensure_host", side_effect=ConnectionError("redis")) as ensure,
		):
			jobs.host_for("default")
			jobs.host_for("long")
			ensure.assert_not_called()
			jobs.host_for("guard-plus")
		ensure.assert_called_once_with("guard-plus")
		with (
			patch("guard.settings.reviews_per_worker", return_value=1),
			patch("guard.review.jobs.ensure_host") as ensure,
		):
			jobs.host_for("guard-plus")
		ensure.assert_not_called()


class TestParserThreads(UnitTestCase):
	def test_each_thread_has_its_own_parser(self):
		parsers = []
		thread = threading.Thread(target=lambda: parsers.append(parser._parser("python")))
		thread.start()
		thread.join()
		self.assertIs(parser._parser("python"), parser._parser("python"))
		self.assertIsNot(parsers[0], parser._parser("python"))
