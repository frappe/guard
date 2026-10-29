"""Create and queue Code Reviews, and the work that runs after one."""

from collections.abc import Callable
from typing import Any

import frappe
from frappe.utils import add_to_date, now_datetime, time_diff_in_seconds
from frappe.utils.background_jobs import get_queue, get_queues_timeout, get_workers, is_job_enqueued

from guard import settings as guard_settings
from guard.review.host import ensure_host, is_hosted
from guard.utils import cache, db

# Dedicated review queues, used when the bench has workers for them (`workers` in
# common_site_config.json). A review with no tier yet starts on the intake queue. The gate picks the
# tier in that job, and the review then moves to the queue of its tier.
INTAKE_QUEUE = "guard-review"
TIER_QUEUES = {"base": "guard-base", "plus": "guard-plus", "apex": "guard-apex"}
REVIEW_QUEUES = (INTAKE_QUEUE, *TIER_QUEUES.values())
# The shared queues, on a bench without a dedicated queue.
SHARED_QUEUES = {"apex": "long"}
# Seconds that a count of the workers of a queue holds: one Redis read a minute, not one per review.
WORKERS_TTL = 60
DEAD_AFTER = 600  # seconds past the job timeout
# Stages that post nothing yet. A review whose job stopped in one of them can start again.
BEFORE_PUBLISH = ("prepare", "start", "index", "context", "review", "verify", "filter")
MAX_ATTEMPT = 5  # restarts and retries of one review, together
RESTARTED = "restarting: the worker stopped during the review"
# A queued review that has had no job for this long lost it. A job that waits in a long queue still
# counts as enqueued, so this only needs to be past the time a request takes to commit.
LOST_AFTER = 300
WAITING_FOR_PUSHES = "waiting for more pushes"
WAITING_FOR_SLOT = "waiting for a free review slot"
WAITING_FOR_LAST = "waiting for the last review to finish"
# A push does not replace a running review in these stages, or in the review stage after half of its
# agent rounds (`agent_round_limit`): most of its tokens are spent. It posts on its commit, and the review of the push waits for
# it and then reads only the changes since.
FINISHING_STAGES = ("verify", "filter")


def create_review(
	pull_request: str,
	trigger: str,
	requested_by: str = "",
	requested_tier: str = "",
	instructions: str = "",
	user: str | None = None,
	status_check: bool | None = None,
	delay: int = 0,
) -> str:
	"""Create a review, queue it, and cancel the older reviews of the pull request.

	The pull request shows the new review at once: a queued check on its head, and the review in
	Desk. `status_check` is None when the caller did not read the config: then the dashboard
	settings of the team decide. A review with a `delay` starts after that many seconds, so that a
	newer push can replace it before it costs anything.
	"""
	from guard.review import state

	head = db.value("Pull Request", pull_request, "head_sha")
	repository = db.value("Pull Request", pull_request, "repository")
	older = frappe.get_all(
		"Code Review",
		filters={"pull_request": pull_request, "status": ["in", ["Queued", "Running"]]},
		fields=["name", "status", "stage", "tier", "agent_rounds", "agent_round_limit"],
	)
	doc = frappe.get_doc(
		{
			"doctype": "Code Review",
			"pull_request": pull_request,
			"repository": repository,
			"source": "pull_request",
			"status": "Queued",
			"trigger": trigger,
			"requested_by": requested_by,
			"requested_tier": requested_tier or "",
			"instructions": instructions,
			"head_sha": head,
			"stage": WAITING_FOR_PUSHES if delay else "",
			"triggered_by_user": user,
			# The check of the commit, when an older review set one: the new review updates it.
			"check_run_id": state.check_of(pull_request, str(head or "")),
		}
	).insert(ignore_permissions=True)
	# After the insert: an older review of the same commit then leaves the check to this one.
	for row in older:
		if trigger == "push" and finishes_first(row):
			continue
		try:
			state.cancel(row.name, state.REPLACED)
		except state.NotActive:
			pass  # It posts its result now. This review comes after it.
	if status_check is None:
		status_check = _dashboard_status_check(str(repository or ""))
	state.queued(str(doc.name), status_check)
	enqueue_review(str(doc.name), delay=delay, tier=requested_tier)
	return str(doc.name)


def finishes_first(row: Any) -> bool:
	"""A push lets this running review finish: it spent most of its tokens. See `FINISHING_STAGES`."""
	if row.status != "Running":
		return False
	if row.stage in FINISHING_STAGES:
		return True
	# The limit of the review itself: a re-review of a push gets fewer turns than its tier.
	limit = row.agent_round_limit or guard_settings.max_turns(row.tier or "base")
	return row.stage == "review" and 2 * (row.agent_rounds or 0) >= limit


def _dashboard_status_check(repository: str) -> bool:
	from guard.guard.doctype.review_settings.review_settings import get_effective

	team = db.value("Code Repository", repository, "team") if repository else None
	return bool(get_effective(team).get("status_check"))


def review_queue(tier: str) -> str:
	"""The queue for a review job of `tier`: its dedicated queue when the bench has one, else a shared one."""
	queue = TIER_QUEUES.get(tier, INTAKE_QUEUE)
	if queue in get_queues_timeout() and has_workers(queue):
		return queue
	return SHARED_QUEUES.get(tier, "default")


def has_workers(queue: str) -> bool:
	"""A worker listens on the queue now. A bench can name a queue in its config and run no worker for
	it, for example before `bench setup supervisor`. A job on that queue would wait for ever."""
	key = f"guard-queue-workers:{queue}"
	known = cache.get(key)
	if known is not None:
		return bool(known)
	try:
		count = len(get_workers(get_queue(queue)))
	except Exception:
		# Redis is down. The shared queue is the safe answer, and the next review looks again.
		return False
	cache.set(key, count, WORKERS_TTL)
	return bool(count)


def handoff_queue(queued_tier: str, tier: str, moved: bool = False) -> str | None:
	"""The queue to move a review to after the gate picked `tier`, or None to go on in this job.

	A job queued with no tier moves, and so does one queued for a requested tier when the gate picks a
	higher one. A job on the intake queue also moves to a shared queue, when its tier has no worker: an
	apex review must not hold the intake worker. On a bench with shared queues only, no job moves. A
	moved job does not move again, also when the gate picks another tier in it.
	"""
	current, queue = review_queue(queued_tier), review_queue(tier)
	if moved or queue == current or not {current, queue} & set(REVIEW_QUEUES):
		return None
	return queue


def enqueue_review(
	review: str, delay: int = 0, attempt: int = 0, tier: str = "", moved: bool = False
) -> None:
	"""Queue a review now, or mark it to resume after `delay` seconds.

	`moved` is for the job that takes over from an intake job, on the queue of the tier.
	"""
	if delay and not frappe.flags.in_test:
		db.set_values(
			"Code Review",
			review,
			{"attempt": attempt, "retry_at": add_to_date(now_datetime(), seconds=delay)},
		)
		return
	if attempt:
		# The job id holds the attempt. `fail_dead_reviews` looks for the job of the saved attempt, so
		# without this it takes a restarted job that runs for a dead one.
		db.set_values("Code Review", review, {"attempt": attempt})
	queue = review_queue(tier)
	frappe.enqueue(
		"guard.review.pipeline.run_review",
		queue=queue,
		timeout=job_timeout(tier),
		job_id=job_id(review, attempt, moved),
		deduplicate=True,
		enqueue_after_commit=True,
		now=bool(frappe.flags.in_test),
		review=review,
		attempt=attempt,
		tier=tier,
		moved=moved,
	)
	if not frappe.flags.in_test:  # the job ran inline
		host_for(queue)


def host_for(queue: str) -> None:
	"""Let a host run the review job just queued, when workers run several reviews.

	Only on the review queues: a host on a shared queue would hold a worker that other jobs need.
	"""
	if queue not in REVIEW_QUEUES or guard_settings.reviews_per_worker() < 2:
		return

	def ensure() -> None:
		try:
			ensure_host(queue)
		except Exception as error:
			# The review job is queued, and a worker runs it alone.
			frappe.logger("guard").warning(f"could not queue a review host on {queue}: {error}")

	after_enqueues(ensure)


def after_enqueues(action: Callable[[], None]) -> None:
	"""Run `action` after the jobs queued at the commit, in the same list as `frappe.enqueue` uses."""
	callbacks = getattr(frappe.local, "deferred_enqueue_after_commit", None)
	(callbacks or frappe.db.after_commit).add(action)


def job_timeout(tier: str) -> int:
	"""Seconds a review job may run. A new review has no tier yet, and the gate can make it apex."""
	return 1800 if tier in ("base", "plus") else 7200


def job_id(review: str, attempt: int, moved: bool = False) -> str:
	"""The RQ job id. A moved job has its own id: the intake job still runs when it is queued."""
	return f"guard-review-{review}-{attempt}" + ("-moved" if moved else "")


def has_job(review: str, attempt: int) -> bool:
	"""A job of this attempt is queued or runs, on the intake queue or on the queue of its tier, or a
	host runs the attempt on a thread."""
	return (
		is_job_enqueued(job_id(review, attempt))
		or is_job_enqueued(job_id(review, attempt, True))
		or is_hosted(review, attempt)
	)


def restart_dead_reviews() -> None:
	"""After a migrate: queue `fail_dead_reviews` now. A site update stops the running review jobs, and
	the dead reviews hold their review slots until the job runs."""
	try:
		frappe.enqueue("guard.review.jobs.fail_dead_reviews", queue="short")
	except Exception as error:
		# Redis can be down in a migrate. The job runs every 5 minutes anyway: do not stop the migrate.
		frappe.logger("guard").warning(f"could not queue fail_dead_reviews after the migrate: {error}")


def fail_dead_reviews() -> None:
	"""Every 5 minutes: start again a running review whose job stopped, for example in a deploy.

	A review that had started to post, or that stopped too often, fails: else it stays Running,
	and its pull request shows "Guard is reviewing" for ever.
	"""
	from guard.review import state
	from guard.review.pipeline import ReviewPipeline

	rows = frappe.get_all(
		"Code Review",
		filters={"status": "Running"},
		fields=["name", "attempt", "tier", "started_at", "stage", "cancel_requested"],
	)
	for row in rows:
		attempt = row.attempt or 0
		alive = has_job(row.name, attempt)
		age = time_diff_in_seconds(now_datetime(), row.started_at) if row.started_at else 0
		if alive and age < job_timeout(row.tier or "") + DEAD_AFTER:
			continue
		if row.cancel_requested and not alive:
			if state.move(row.name, {"status": "Cancelled", "completed_at": now_datetime()}):
				state.notify(row.name)
				db.commit()
				state.show_cancelled(row.name)
			continue
		if not alive and row.stage in BEFORE_PUBLISH and attempt + 1 < MAX_ATTEMPT:
			if state.move(row.name, {"status": "Queued", "stage": RESTARTED}, ("Running",)):
				enqueue_review(row.name, attempt=attempt + 1, tier=row.tier or "")
			continue
		pipeline = ReviewPipeline(row.name)
		pipeline.fail("The review job stopped before the review finished, for example in a restart.")
		pipeline.acknowledge()


def retry_review(review: str) -> bool:
	"""Run a failed or cancelled review again, from the start. False when it is in another state."""
	from guard.review import state

	tier = db.value("Code Review", review, "tier")
	if not state.move(
		review,
		{
			"status": "Queued",
			"stage": "",
			"error": None,
			"attempt": 0,
			"retry_at": None,
			"completed_at": None,
			"cancel_requested": 0,
			"cancelled_by": None,
		},
		("Failed", "Cancelled"),
	):
		return False
	repository = str(db.value("Code Review", review, "repository") or "")
	state.queued(review, _dashboard_status_check(repository))
	enqueue_review(review, attempt=0, tier=str(tier or ""))
	return True


def resume_waiting() -> None:
	"""Every minute: queue reviews whose retry time has passed, and queued reviews that lost their job."""
	rows = frappe.get_all(
		"Code Review",
		filters=[["status", "=", "Queued"], *db.due("retry_at")],
		fields=["name", "attempt", "tier", "stage"],
	)
	from guard.review import state

	for row in rows:
		values: dict[str, Any] = {"retry_at": None}
		if row.stage == WAITING_FOR_PUSHES:
			values["stage"] = ""  # it waits for a worker now
		# Under the row lock: a review cancelled since the select stays cancelled.
		if not state.move(row.name, values, allowed=("Queued",)):
			continue
		enqueue_review(row.name, attempt=row.attempt or 0, tier=row.tier or "")
	requeue_lost()


def requeue_lost() -> None:
	"""Queue again a Queued review that has no job. The Code Review is the truth, not the queue: a
	request that died before its commit hooks ran, or a Redis that lost its jobs, leaves one."""
	rows = frappe.get_all(
		"Code Review",
		filters={
			"status": "Queued",
			"retry_at": ["is", "not set"],
			"modified": ["<", add_to_date(now_datetime(), seconds=-LOST_AFTER)],
		},
		fields=["name", "attempt", "tier"],
	)
	for row in rows:
		attempt = row.attempt or 0
		if not has_job(row.name, attempt):
			enqueue_review(row.name, attempt=attempt, tier=row.tier or "")


STOPPED = "Guard stopped before it finished this review. Ask for a new review if you need one."


def close_stuck_checks(days: int = 1) -> int:
	"""Every hour: complete the checks that say "Guard is reviewing" on a commit no review runs on.

	A killed job, or an id that the cache lost, leaves one. Return how many were closed.
	"""
	from guard.providers import adapter_for_repository

	rows = db.dict_rows(
		"""SELECT r.repository, c.full_name, r.head_sha FROM `tabCode Review` r
		JOIN `tabCode Repository` c ON c.name = r.repository
		WHERE r.source = 'pull_request' AND r.head_sha IS NOT NULL AND r.head_sha != '' AND r.creation > %s
		GROUP BY r.repository, c.full_name, r.head_sha
		HAVING SUM(r.status IN ('Queued', 'Running')) = 0""",
		(add_to_date(now_datetime(), days=-days),),
	)
	adapters: dict[str, Any] = {}
	closed = 0
	for row in rows:
		repository = str(row["repository"])
		if repository not in adapters:
			adapters[repository] = adapter_for_repository(repository)
		adapter = adapters[repository]
		if not adapter:
			continue
		try:
			closed += adapter.close_open_checks(str(row["full_name"]), str(row["head_sha"]), STOPPED)
		except Exception as error:
			frappe.log_error(str(error)[:2000], "Guard checks")
	return closed


def after_review(review: str) -> None:
	"""Auto-approve and tour, after the review is saved."""
	from guard.conversation.tour import build_tour
	from guard.learning.approve import maybe_approve

	maybe_approve(review)
	build_tour(review)
