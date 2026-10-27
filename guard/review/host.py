"""Run several reviews in one worker, each on its own thread.

A review spends most of its time waiting for the AI gateway. With `reviews_per_worker` above 1, a
host job takes review jobs off the dedicated review queue that it runs on, and runs each one on a
thread with its own site connection, up to that many at once. An idle worker still takes a review
job itself, so a host only takes the jobs that would wait for a worker.

A taken job leaves RQ: its id is free again, so a park or a retry can queue the review again. A
Redis mark tells `jobs.has_job` that the review runs. The host refreshes it, and it expires when the
worker dies, so `fail_dead_reviews` starts the review again.

The host state is in plain Redis keys, not in `guard.utils.cache`: that keeps values in
`frappe.local` for the life of the job, and a host runs for hours.
"""

import contextvars
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

import frappe
from frappe.utils.background_jobs import execute_job, get_job, get_queue
from rq import get_current_job
from rq.job import Job, JobStatus
from rq.queue import Queue
from rq.timeouts import BaseTimeoutException

from guard import settings as guard_settings
from guard.utils import db

METHOD = "guard.review.pipeline.run_review"
# Seconds a host job may run. It takes a review only when the review fits in the time left, so a
# host of 4 hours takes apex reviews (2 hours) in its first 2 hours, and the others for longer.
HOST_TIMEOUT = 4 * 3600
MAX_HOSTS = 16  # host jobs on one queue at once
POLL = 5  # seconds between two looks at the queue and the threads
SCAN = 50  # jobs at the front of the queue that a host looks at
ALIVE_TTL = 120  # seconds that the mark of a running review lives without a refresh
FREE_TTL = 30  # seconds that the free thread count of a host lives without a refresh
MARGIN = 60  # seconds a host keeps free at the end of its job

# Take one free thread of a host, when it has one. Atomic, so two requests do not take the same one.
RESERVE = """
local free = tonumber(redis.call('GET', KEYS[1]) or '0')
if free > 0 then return redis.call('DECR', KEYS[1]) end
return -1
"""
# Delete the mark only when it is still this thread's: a newer job of the review can have set it.
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
# Renew the mark, unless a newer job of the review owns it now.
RENEW = """
local mark = redis.call('GET', KEYS[1])
if mark and mark ~= ARGV[1] then return 0 end
return redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
"""


@dataclass
class Hosted:
	review: str
	attempt: int
	thread: threading.Thread
	token: str = field(default_factory=lambda: frappe.generate_hash(length=12))


def alive_key(review: str, attempt: int) -> str:
	return f"guard-hosted-review:{review}:{attempt}"


def is_hosted(review: str, attempt: int) -> bool:
	"""A host runs this attempt of the review now."""
	return bool(_redis().exists(alive_key(review, attempt)))  # `exists` makes the key itself


def host_job_id(queue: str, index: int) -> str:
	return f"guard-review-host-{queue}-{index}"


def free_key(queue: str, index: int) -> str:
	return f"guard-review-host-free:{queue}:{index}"


def ensure_host(queue: str) -> None:
	"""Make sure that a host can take the review job just queued on `queue`.

	A queued host takes it when it starts, and a running host with a free thread at its next look.
	Else queue a new host, at the front: the next free worker becomes a host, not a single review.
	"""
	# Once in a request: the host is queued at the commit, so a second look would not see it yet.
	ensured: set[str] = frappe.flags.setdefault("guard_hosts_ensured", set())
	if queue in ensured:
		return
	ensured.add(queue)
	unused: list[int] = []
	for index in range(MAX_HOSTS):
		job = get_job(host_job_id(queue, index))
		status = job.get_status(refresh=False) if job else None
		if status == JobStatus.QUEUED:
			return
		if status == JobStatus.STARTED and job is not None:
			if reserve(queue, index):
				return
			if not _host_is_dead(job, queue, index):
				continue
			# RQ keeps the job of a killed worker as started for hours. Its index is free again.
			job.delete()
		unused.append(index)
	if not unused:
		return
	frappe.enqueue(
		"guard.review.host.serve",
		queue=queue,
		timeout=HOST_TIMEOUT,
		job_id=host_job_id(queue, unused[0]),
		deduplicate=True,
		at_front=True,
		enqueue_after_commit=True,
		queue_name=queue,
		index=unused[0],
	)


def reserve(queue: str, index: int) -> bool:
	"""Take a free thread of the host: the next review job of this queue is for it."""
	return int(_redis().eval(RESERVE, 1, _key(free_key(queue, index)))) >= 0


def _host_is_dead(job: Job, queue: str, index: int) -> bool:
	"""A started host that has not told its free threads for a while. A new one tells at once."""
	if _redis().exists(free_key(queue, index)):
		return False
	started = job.started_at.timestamp() if job.started_at else 0
	return time.time() - started > FREE_TTL * 2


def serve(queue_name: str, index: int = 0) -> None:
	"""Background job: run review jobs of `queue_name` on threads until none is left."""
	job = get_current_job()
	timeout = int(job.timeout) if job is not None and job.timeout else HOST_TIMEOUT
	Host(
		get_queue(queue_name), guard_settings.reviews_per_worker(), timeout, free_key(queue_name, index)
	).serve()


class Host:
	def __init__(self, queue: Queue, size: int, timeout: int, free: str = ""):
		self.queue = queue
		self.size = size
		self.ends_at = time.time() + timeout - MARGIN
		self.free = free
		self.site = str(frappe.local.site)
		self.running: list[Hosted] = []

	def serve(self) -> None:
		# This job reads nothing more from the database. An open transaction for hours holds back
		# the purge of old row versions, and the server can close an idle connection. Frappe
		# connects again for the commit at the end of the job.
		db.rollback()
		db.close()
		try:
			while True:
				took = self.step()
				if not self.running and not took:
					return
				time.sleep(POLL)
		finally:
			if self.free:
				_redis().delete(_key(self.free))

	def step(self) -> bool:
		"""One look at the threads and the queue. True when a review started.

		An error here must not end the job: its threads, and so the reviews on them, end with it.
		"""
		try:
			self.reap()
			took = self.fill()
			self.mark()
			return took
		except BaseTimeoutException:
			raise
		except Exception:
			frappe.logger("guard").exception("review host: a look at the queue failed")
			return False

	def fill(self) -> bool:
		"""Start reviews on the free threads. True when one started."""
		took = False
		while len(self.running) < self.size and (taken := self.take()):
			self.start(*taken)
			took = True
		return took

	def take(self) -> tuple[Job, Hosted] | None:
		"""Take the first review job of this site that fits in the time left, off the queue."""
		left = self.ends_at - time.time()
		for job_id in self.queue.get_job_ids(0, SCAN):
			try:
				job = self.queue.fetch_job(job_id)
				if job is None:
					continue
				queued = job.kwargs
			except Exception:
				continue  # a job that does not load is not a review job of this host
			if queued.get("site") != self.site or queued.get("method") != METHOD:
				continue
			if (job.timeout or 0) > left:
				continue
			# LREM is atomic: of two hosts, or a host and a worker, only one gets the job.
			if not self.queue.remove(job_id):
				continue
			review = queued.get("kwargs") or {}
			hosted = Hosted(str(review.get("review")), int(review.get("attempt") or 0), threading.Thread())
			key = _key(alive_key(hosted.review, hosted.attempt))
			try:
				_redis().set(key, hosted.token, ex=ALIVE_TTL)
				# Free the id: a park or a retry of the review queues a job with the same id.
				job.delete(remove_from_queue=False)
			except Exception:
				# Put the job back. A job that is out of the queue but still queued in RQ counts as
				# enqueued for good, so no cron would queue the review again.
				self.queue.push_job_id(job_id, at_front=True)
				_redis().eval(RELEASE, 1, key, hosted.token)
				raise
			return job, hosted
		return None

	def start(self, job: Job, hosted: Hosted) -> None:
		queued = dict(job.kwargs)
		review = queued.get("kwargs") or {}
		# The review stops its agents in time for the timeout of its own job, not of this one.
		queued["kwargs"] = {**review, "timeout": int(job.timeout or 0)}
		hosted.thread = threading.Thread(
			target=run_job,
			args=(queued,),
			name=f"guard-review-{hosted.review}",
			daemon=True,
			# A new context: the thread connects to the site itself, and shares no `frappe.local`.
			context=contextvars.Context(),
		)
		hosted.thread.start()
		self.running.append(hosted)

	def reap(self) -> None:
		for hosted in [h for h in self.running if not h.thread.is_alive()]:
			_redis().eval(RELEASE, 1, _key(alive_key(hosted.review, hosted.attempt)), hosted.token)
			self.running.remove(hosted)

	def mark(self) -> None:
		"""Keep the marks of the running reviews, and tell `ensure_host` how many threads are free."""
		for hosted in self.running:
			_redis().eval(RENEW, 1, _key(alive_key(hosted.review, hosted.attempt)), hosted.token, ALIVE_TTL)
		if self.free:
			free = self.size - len(self.running) if time.time() < self.ends_at else 0
			_redis().set(_key(self.free), free, ex=FREE_TTL)


def run_job(queued: dict[str, Any]) -> None:
	"""Run a taken job on this thread, as a worker runs it: connect, run, commit, log an error."""
	try:
		execute_job(**queued)
	except Exception:
		# `execute_job` logged it in the Error Log, unless it failed to connect. The worker log has it.
		traceback.print_exc()


def _redis() -> Any:
	return frappe.cache


def _key(name: str) -> str:
	return _redis().make_key(name)
