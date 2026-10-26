"""Run one Code Review from start to end. See docs/spec/05-review-engine.md."""

import dataclasses
import time
from typing import TYPE_CHECKING, Any, cast

import frappe
from frappe.utils import now_datetime

from guard import settings as guard_settings
from guard.config import gate
from guard.config.resolver import ConfigResolver
from guard.git.diff import FileDiff, parse_diff
from guard.git.repository import store_for
from guard.git.store import GitStore
from guard.graph.overlay import PullRequestGraph
from guard.graph.queries import CodeGraph
from guard.indexing import frappe_plugin
from guard.indexing.indexer import IndexBusy, IndexFailed
from guard.indexing.jobs import ensure_fresh
from guard.integrations import tickets
from guard.learning import suppression
from guard.llm import breaker
from guard.llm.types import GatewayBusy, GatewayDown, Usage
from guard.providers import adapter_for_repository
from guard.review import (
	checkpoint,
	checks,
	context,
	crossrepo,
	links,
	prompts,
	rule_checks,
	rule_files,
	sandbox,
	stale,
	state,
	verify,
)
from guard.review.agent import Agent, run_together, tools
from guard.review.findings import FindingFilter, score_cap
from guard.review.publish import Publisher, remove_eyes, set_status
from guard.review.run import ReviewRun, Verdict
from guard.review.task import compose
from guard.utils import db
from guard.utils.redact import redact

if TYPE_CHECKING:
	from guard.guard.doctype.code_review.code_review import CodeReview
	from guard.guard.doctype.pull_request.pull_request import PullRequest
	from guard.providers.base import ProviderAdapter

AREA_FILES = 8
# Parts of an apex review. A larger pull request gets parts for its riskiest files, and the pass
# across the parts reviews the rest: the cost stays bounded.
MAX_AREAS = 6
# Time kept after the agents for verification, stale checks and publishing.
FINISH_SECONDS = 360
MAX_REST_NAMES = 60
RETRY_DELAYS = (60, 300, 900)
# Rate limits on the gateway last up to an hour. These waits add up to 72 minutes.
BUSY_DELAYS = (120, 600, 1200, 2400)
SLOT_WAIT = 60  # seconds a review waits for a free slot before it looks again
GATEWAY_DOWN = "paused: the AI gateway is down"


class Cancelled(Exception):
	pass


class JobReplaced(Exception):
	"""A newer job runs the same review. This job stops and writes nothing more to the review."""


class ReviewPipeline:
	def __init__(self, review: str):
		self.review = cast("CodeReview", frappe.get_doc("Code Review", review))
		self.started = time.time()
		self.current: ReviewRun | None = None  # the run of this attempt, once prepared
		self.busy: GatewayBusy | None = None  # why this attempt went back to the queue, if it did
		# Written to `job_id` when this job starts the review. A deploy can start a second job of the
		# same review, with the same RQ job id, while the first still runs. The newest claim wins.
		self.claim: str | None = None
		self.job_replaced = False
		# What the checkpoint of this run must match, and the agents it saves: a phase and its agents.
		self.checkpoint_key: str | None = None
		self.watched: tuple[str, list[Agent]] = ("", [])
		# Seconds in each stage, over all attempts, and the stage that runs now with its start.
		self.stage_seconds: dict[str, float] = {}
		self.stage_name = ""
		self.stage_started = 0.0

	@classmethod
	def run(cls, review: str, attempt: int = 0) -> "ReviewPipeline":
		pipeline = cls(review)
		try:
			pipeline.attempt(attempt)
		finally:
			if not pipeline.job_replaced:
				pipeline.acknowledge()
				pipeline.drop_checkpoint()
		return pipeline

	def drop_checkpoint(self) -> None:
		"""A review that ended does not start again. A queued one keeps its checkpoint."""
		if db.value("Code Review", str(self.review.name), "status") not in ("Queued", "Running"):
			checkpoint.delete(str(self.review.name))

	def acknowledge(self) -> None:
		"""Finish the mentions that wait for a review: 👍 when this one completed, 😕 when it did not.

		A push can replace the review that a mention asked for. Then the newer review finishes them.
		"""
		status = db.value("Code Review", str(self.review.name), "status")
		if status in state.ACTIVE or not self.review.pull_request:
			return
		pull_request = str(self.review.pull_request)
		if status != "Completed" and state.newer_review_runs(
			pull_request, str(self.review.name), self.review.creation
		):
			return
		from guard.conversation import acks

		acks.review_finished(pull_request, failed=status != "Completed")

	def attempt(self, attempt: int) -> None:
		try:
			# Before a park: else a cancelled review stays Queued while it waits for the gateway or a slot.
			self.check_cancel()
			if wait := breaker.wait_seconds():
				self.park(attempt, wait, GATEWAY_DOWN)
				return
			if self.slots_full():
				self.park(attempt, SLOT_WAIT, "waiting for a free review slot")
				return
			self.execute()
		except JobReplaced:
			db.rollback()
			self.job_replaced = True
			frappe.logger("guard").info(f"review {self.review.name}: a newer job took it over")
		except Cancelled:
			db.rollback()
			name = str(self.review.name)
			# A cancel saves the status itself. Only a cancel from before that sets it here.
			if state.move(name, {"status": "Cancelled", "completed_at": now_datetime(), "retry_at": None}):
				state.notify(name)
			db.commit()
			# Again: this job can have set the check between two steps after the cancel.
			if db.value("Code Review", name, "status") == "Cancelled":
				state.show_cancelled(name)
		except GatewayDown as down:
			breaker.record_failure(str(down))
			if wait := breaker.wait_seconds():
				self.park(attempt, wait, GATEWAY_DOWN)
				return
			self.retry(attempt, down.retry_after, "the AI gateway", f"{breaker.FAILED_PREFIX}: {down}")
		except GatewayBusy as busy:
			self.busy = busy
			self.retry(attempt, busy.retry_after, "the AI gateway", f"AI gateway busy: {busy}", BUSY_DELAYS)
		except IndexBusy:
			# Do not index the branch a second time, in parallel.
			self.retry(attempt, 0, "the code graph index", "The code graph index stayed busy.")
		except Exception as error:
			db.rollback()
			# The error field keeps one line. The traceback tells where it failed.
			frappe.log_error(
				"Guard review failed", redact(frappe.get_traceback()), "Code Review", self.review.name
			)
			self.fail(redact(f"{type(error).__name__}: {error}"))
			if frappe.flags.in_test:
				raise
		finally:
			try:
				# The newer job keeps its own count, which includes this one's.
				if not self.job_replaced:
					self.save_usage()
			except Exception as error:
				frappe.log_error(redact(str(error)), "Guard review usage")

	def slots_full(self) -> bool:
		"""As many reviews run as `max_concurrent_reviews` allows.

		Two reviews that start at the same moment can both pass, so the limit can be passed by one
		or two. It stops a burst of pull requests from taking every worker and all the memory.
		"""
		limit = guard_settings.max_concurrent_reviews()
		if not limit:
			return False
		running = db.scalar(
			"SELECT COUNT(*) FROM `tabCode Review` WHERE status = 'Running' AND name != %s",
			(str(self.review.name),),
		)
		return int(running or 0) >= limit

	def park(self, attempt: int, wait: int, stage: str) -> None:
		"""Wait without using up a retry: the gateway is down, or every review slot is taken."""
		if not state.move(str(self.review.name), {"status": "Queued", "stage": stage}):
			db.commit()  # Let go of the row lock.
			return
		db.commit()
		from guard.review.jobs import enqueue_review

		enqueue_review(str(self.review.name), delay=wait, attempt=attempt)

	def retry(
		self,
		attempt: int,
		after: int,
		waiting_for: str,
		failure: str,
		delays: tuple[int, ...] = RETRY_DELAYS,
	) -> None:
		delay = max(after, delays[min(attempt, len(delays) - 1)])
		if attempt >= len(delays):
			self.fail(failure)
			return
		if not state.move(
			str(self.review.name), {"status": "Queued", "stage": f"waiting {delay}s for {waiting_for}"}
		):
			db.commit()  # Let go of the row lock.
			return
		db.commit()
		from guard.review.jobs import enqueue_review

		enqueue_review(str(self.review.name), delay=delay, attempt=attempt + 1)

	# ---------------------------------------------------------------- stages

	def execute(self) -> None:
		run = self.prepare()
		if run is None:
			return
		# Earlier attempts of this review count too: their tokens were spent.
		run.usage.add(
			Usage(
				int(self.review.input_tokens or 0),
				int(self.review.output_tokens or 0),
				int(self.review.cached_tokens or 0),
			)
		)
		run.turns = int(self.review.agent_turns or 0)
		run.rounds = int(self.review.agent_rounds or 0)
		self.current = run
		publisher = Publisher(run)
		self.stage("start")
		publisher.start()
		self.stage("index")
		index = self.index(run)
		run.graph = CodeGraph(index) if index else None
		self.stage("context")
		overlay = PullRequestGraph(run.graph, run.head, run.base, run.files, run.frappe_app)
		run.changed = overlay.build()
		others = self.linked_repositories(run)
		crossrepo.attach(run.changed, others, index)
		run.blast = overlay.blast_radius(run.changed)
		if run.settings.get("include_rule_files", True):
			run.rule_files = rule_files.collect(run.paths, run.head, run.list_dir)
		self.pick_tier(run)
		if not self.credits_allow(run):
			return
		linked = (
			tickets.for_pull_request(run.title, run.description, run.head_branch)
			if run.pr is not None
			else []
		)
		run.context_used = [{"title": f"{t.key} {t.title}", "url": t.url, "source": t.source} for t in linked]
		team = db.value("Code Repository", run.repository, "team") if run.repository else None
		built = context.build(
			run.head,
			run.changed,
			run.blast,
			run.graph,
			run.cfg,
			run.paths,
			linked,
			run.full_name,
			team,
			run.repository,
			run.rule_files,
		)
		self.stage("review")
		run.findings += checks.secret_findings(run.files)
		run.findings += crossrepo.missing_targets(run.files, run.head, others, index)
		run.findings += crossrepo.break_findings(run.changed)
		rules = rule_files.project_rules(run.rule_files)
		run.findings += checks.missing_test_findings(
			run.files, [f.path for f in run.all_files if f.status != "deleted"], rules
		)
		run.findings += rule_checks.findings(run.files, run.head, run.base, rules)
		if run.frappe_app:
			run.findings += checks.frappe_findings(run.files)
		run.sandbox = sandbox.Sandbox(run) if sandbox.wanted(run) else None
		try:
			self.agent(run, built)
		finally:
			if run.sandbox is not None:
				run.sandbox.cleanup()
		# A job that stops after this point does not run the agents again.
		self.watched = ("done", [])
		self.save_checkpoint()
		self.stage("verify")
		verify.verify(run)
		self.stage("filter")
		existing = self.existing_findings()
		finding_filter = FindingFilter(run.files, run.settings, existing, run.fresh_lines)
		finding_filter.run(run.findings)
		suppression.suppress(run.findings, run.repository)
		# Before the score: the earlier findings that still apply count, as on the status check.
		stale.close_stale(run, finding_filter.reraised)
		# Only the model's verdict: the graph verdict below does not rest on findings.
		verify.check_verdict(run)
		if not run.verdict.submitted:
			run.verdict = graph_verdict(run)
		earlier = [row["severity"] for row in run.earlier_open]
		run.verdict.confidence_score = min(run.verdict.confidence_score, score_cap(run.findings, earlier))
		self.stage("publish")
		names = publisher.save_findings()
		number = self.review_number()
		publisher.publish(names, number)
		publisher.finish()
		self.stage("after")
		suppression.remember(names, run.findings, run.repository)
		self.after(run, number)

	def index(self, run: ReviewRun) -> str | None:
		"""The fresh index of the repository, or None when it failed: review without the graph."""
		if not run.repository:
			return None
		try:
			return ensure_fresh(run.repository)
		except IndexFailed as error:
			run.notes.append(
				"The code graph is not available, so this review read the diff and the changed files only. "
				f"Indexing failed: {redact(str(error))[:200]}"
			)
			return None

	def linked_repositories(self, run: ReviewRun) -> list[crossrepo.OtherRepository]:
		if not run.repository:
			return []
		provider = db.value("Code Repository", run.repository, "code_provider")
		return crossrepo.linked(run.repository, str(provider or ""), run.cfg.repos)

	def prepare(self) -> ReviewRun | None:
		self.claim = frappe.generate_hash(length=12)
		running = {
			"status": "Running",
			"started_at": now_datetime(),
			"stage": "prepare",
			"job_id": self.claim,
		}
		if not state.move(str(self.review.name), running):
			raise Cancelled
		db.commit()
		self.review.reload()
		saved = frappe.parse_json(self.review.stage_seconds or "{}")
		self.stage_seconds = {str(k): float(v) for k, v in saved.items()} if isinstance(saved, dict) else {}
		self.stage_name, self.stage_started = "prepare", time.time()
		if self.review.source == "cli":
			from guard.review.cli_review import prepare_cli

			return prepare_cli(self.review)
		pr = cast("PullRequest", frappe.get_doc("Pull Request", str(self.review.pull_request)))
		repository = str(pr.repository)
		number = int(pr.number)
		full_name = str(db.value("Code Repository", repository, "full_name"))
		adapter = adapter_for_repository(repository)
		store = store_for(repository)
		store.fetch_branches(pr.base_branch or "")
		if adapter and adapter.pull_ref(number):
			head = store.fetch_pull(adapter.pull_ref(number), number)
		else:
			head = store.fetch_branch(pr.head_branch or "", number, pr.head_clone_url or None)
		base_ref = f"refs/heads/{pr.base_branch}"
		merge_base = store.merge_base(store.rev_parse(base_ref), head)
		files = parse_diff(store.diff(merge_base, head))
		resolver = ConfigResolver.from_store(repository, store, head)
		all_paths = [f.path for f in files]
		config = resolver.resolve_pr(all_paths)
		reviewed = set(config.reviewed_paths())
		run = ReviewRun(
			review=self.review,
			repository=repository,
			full_name=full_name,
			head=lambda path: store.show(head, path),
			base=lambda path: store.show(merge_base, path),
			list_dir=lambda path: store.list_dir(head, path),
			head_sha=head,
			base_sha=merge_base,
			grep=lambda pattern, glob: store.grep(head, pattern, glob),
			title=pr.title or "",
			description=pr.description or "",
			author=pr.author or "",
			labels=list(frappe.parse_json(pr.labels or "[]") or []),
			base_branch=pr.base_branch or "",
			head_branch=pr.head_branch or "",
			pr=pr,
			adapter=adapter,
			all_files=files,
			files=[f for f in files if f.path in reviewed and not f.is_binary],
			config=config,
			commits=store.commits_between(merge_base, head),
			instructions=self.review.instructions or "",
			frappe_app=frappe_plugin.is_frappe_app({e.path for e in store.ls_tree(head)}),
		)
		run.notes = list(config.errors)
		run.fresh_lines = self.get_fresh_lines(store, pr.last_reviewed_sha or "", head)
		if adapter and self.review.head_sha and self.review.head_sha != head:
			self.leave_old_check(adapter, full_name, str(self.review.head_sha), head)
		self.review.db_set(
			{
				"head_sha": head,
				"merge_base_sha": merge_base,
				"base_sha": pr.base_sha,
				"strictness": config.settings["strictness"],
				"changed_files": frappe.as_json([{"path": f.path, "status": f.status} for f in files]),
			}
		)
		if not run.files:
			if not state.move(
				str(self.review.name),
				{"status": "Skipped", "error": "no reviewable files", "completed_at": now_datetime()},
			):
				raise Cancelled
			self.skip_check(run, "it has no files to review")
			return None
		return run

	def pick_tier(self, run: ReviewRun) -> None:
		facts = gate.PullRequestFacts(
			repository=run.full_name,
			title=run.title,
			description=run.description,
			author=run.author,
			base_branch=run.base_branch,
			head_branch=run.head_branch,
			labels=run.labels,
			paths=[p for f in run.files for p in {f.path, f.old_path} if p],
			additions=sum(f.additions for f in run.files),
			deletions=sum(f.deletions for f in run.files),
		)
		from guard.guard.doctype.review_settings.review_settings import get_effective

		team = db.value("Code Repository", run.repository, "team") if run.repository else None
		rules = gate.dashboard_rules(get_effective(team))
		tier_rules = [r.review_tier for r in rules if r.review_tier and gate.rule_matches(r, facts)]
		contracts = sum(1 for e in run.changed if e.contract_changed)
		run.tier = gate.select_tier(
			run.cfg,
			tier_rules,
			self.review.requested_tier or None,
			facts,
			contracts,
			first_review=self.review_number() == 1,
		)
		self.review.db_set("tier", run.tier)

	def credits_allow(self, run: ReviewRun) -> bool:
		cap = int(guard_settings.settings().monthly_credit_cap or 0)
		if not cap:
			return True
		used = int(
			db.scalar(
				"SELECT IFNULL(SUM(credits), 0) FROM `tabCode Review` WHERE creation >= DATE_FORMAT(NOW(), '%%Y-%%m-01') AND status = 'Completed'"
			)
			or 0
		)
		if used + guard_settings.CREDITS[run.tier] <= cap:
			return True
		if not state.move(
			str(self.review.name),
			{"status": "Skipped", "error": "monthly credit cap reached", "completed_at": now_datetime()},
		):
			raise Cancelled
		self.skip_check(run, "the monthly credit cap is reached")
		if run.adapter and run.pr is not None:
			run.adapter.post_comment(
				run.full_name,
				int(run.pr.number),
				"Guard skipped this review: the monthly credit cap is reached.",
			)
		return False

	def agent(self, run: ReviewRun, built: context.BuiltContext) -> None:
		from guard.guard.doctype.review_settings.review_settings import get_effective

		team = db.value("Code Repository", run.repository, "team") if run.repository else None
		risk = str(get_effective(team).get("risk_definitions") or "")
		system = prompts.system_prompt(int(run.settings.get("strictness") or 2), "", risk)
		turns = guard_settings.max_turns(run.tier)
		deadline = self.deadline(run.tier)
		single = run.tier != "apex" or len(run.files) <= AREA_FILES
		ranked = [] if single else by_risk(run)
		areas = [ranked[i : i + AREA_FILES] for i in range(0, len(ranked), AREA_FILES)][:MAX_AREAS]
		saved = self.open_checkpoint(run, system, [[f.path for f in files] for files in areas])
		phase = saved["phase"] if saved else ""
		if saved and phase == "done":
			run.verdict = Verdict(**saved["verdict"])
			return
		if single:
			final = Agent(run, system, turns)
			task = None if self.restore(final, saved, "final") else compose(run, built)
			self.watched = ("final", [final])
			final.run_loop(task, deadline, self.between_turns)
			return
		# Each part, and the pass across the parts, reviews less than the whole: half the turns.
		turns = max(turns // 2, guard_settings.max_turns("base"))
		all_files = run.files
		in_areas = {f.path for files in areas for f in files}
		rest = [f for f in ranked if f.path not in in_areas]
		extra = ""
		if phase != "final":
			tasks: list[str | None] = []
			parts = [Agent(run, system, turns, final=False) for _ in areas]
			for number, (part, files) in enumerate(zip(parts, areas, strict=True), 1):
				run.files = files
				what = f"Review only the files in this diff part ({number}). Submit a verdict for this part."
				restored = self.restore(part, saved, "parts", number - 1)
				tasks.append(None if restored else compose(run, built, what))
			self.watched = ("parts", parts)
			# The pass across the parts needs time too: the parts may use two thirds of what is left.
			areas_until = time.time() + (deadline - time.time()) * 2 / 3
			results = run_together(parts, tasks, self.between_turns, areas_until)
			reported = "\n".join(f"- {f.path}:{f.start_line} {f.title}" for f in run.findings)
			verdicts = "\n".join(
				f"- part {n} (score {r.verdict.confidence_score}): {r.verdict.summary[:600]}"
				for n, r in enumerate(results, 1)
				if r.verdict.submitted
			)
			extra = prompts.CROSS_CUTTING + "\n\nVerdicts of the parts:\n" + (verdicts or "- none")
			extra += "\n\nAlready reported:\n" + (reported or "- none")
			if rest:
				extra += "\n\nNo part reviewed these files. Review them here:\n" + "\n".join(
					f"- {f.path}" for f in rest[:MAX_REST_NAMES]
				)
		# The files that no part reviewed come first, so the diff limit cuts the reviewed ones.
		run.files = rest + [f for f in ranked if f.path in in_areas]
		final = Agent(run, system, turns)
		task = None if self.restore(final, saved, "final") else compose(run, built, extra)
		self.watched = ("final", [final])
		self.check_cancel()
		final.run_loop(task, deadline, self.between_turns)
		run.files = all_files

	def open_checkpoint(self, run: ReviewRun, system: str, areas: list[list[str]]) -> dict[str, Any] | None:
		"""Start the checkpoints of this run. Return the saved one when it matches, with its findings in the run."""
		self.checkpoint_key = None
		self.watched = ("", [])
		# The sandbox does not keep the files that the agents wrote, so a review with it starts over.
		if run.review is None or run.sandbox is not None:
			return None
		self.checkpoint_key = checkpoint.key(run, system, tools(run), areas)
		saved = checkpoint.load(str(run.review.name), self.checkpoint_key)
		if saved:
			# The checks before the agents found the same at the same head: these replace them.
			run.findings = checkpoint.load_findings(saved["findings"])
			frappe.logger("guard").info(
				f"review {run.review.name}: going on from the {saved['phase']} checkpoint"
			)
		return saved

	@staticmethod
	def restore(agent: Agent, saved: dict[str, Any] | None, phase: str, index: int = 0) -> bool:
		"""Put the saved agent back when the checkpoint is of this phase. It then has its task already."""
		if not saved or saved["phase"] != phase:
			return False
		checkpoint.restore_agent(agent, saved["agents"][index])
		return True

	def save_checkpoint(self) -> None:
		"""Write the agents of this run, so that the next job of the review goes on from here."""
		run = self.current
		if not self.checkpoint_key or run is None or run.review is None:
			return
		phase, agents = self.watched
		data = {
			"key": self.checkpoint_key,
			"phase": phase,
			"agents": [checkpoint.dump_agent(a) for a in agents],
			"findings": checkpoint.dump_findings(run.findings),
			"verdict": dataclasses.asdict(run.verdict),
		}
		try:
			checkpoint.save(str(run.review.name), data)
		except OSError as error:
			# A review without a checkpoint still runs. Only a restart costs more.
			frappe.log_error(redact(str(error)), "Guard review checkpoint")

	def deadline(self, tier: str) -> float:
		"""The time after which the agents must submit, so that the job ends before its timeout.

		A job that the worker kills loses all of its work. The timeout is the one that the job got,
		because a review queued before the gate picked its tier can have a shorter one.
		"""
		from rq import get_current_job

		from guard.review.jobs import job_timeout

		job = get_current_job()
		seconds = job_timeout(tier)
		if job is not None and job.timeout:
			seconds = min(seconds, int(job.timeout))
		return self.started + seconds - FINISH_SECONDS

	def after(self, run: ReviewRun, number: int) -> None:
		posted = [f for f in run.findings if f.kept]
		# The Pull Request row first, as the webhook handler locks it: the other order deadlocks.
		if run.pr is not None:
			db.value_for_update("Pull Request", str(run.pr.name), "name")
		completed = state.move(
			str(self.review.name),
			{
				"status": "Completed",
				"completed_at": now_datetime(),
				"duration": round(time.time() - self.started, 1),
				"summary": run.verdict.summary,
				"confidence_score": run.verdict.confidence_score,
				"confidence_reason": run.verdict.confidence_reason,
				"risk_level": run.verdict.risk_level,
				"risk_reason": run.verdict.risk_reason,
				"diagram_type": "" if run.verdict.diagram_type == "none" else run.verdict.diagram_type,
				"diagram": run.verdict.diagram,
				"important_files": frappe.as_json(run.verdict.important_files),
				"context_used": frappe.as_json(run.context_used),
				"config_snapshot": frappe.as_json(run.settings),
				"files_reviewed": len(run.files),
				"findings_count": len(run.findings),
				"posted_count": len(posted),
				"suppressed_count": sum(1 for f in run.findings if f.status == "Suppressed"),
				"credits": guard_settings.CREDITS.get(run.tier, 1),
				"input_tokens": run.usage.input_tokens,
				"output_tokens": run.usage.output_tokens,
				"cached_tokens": run.usage.cached_tokens,
				"agent_turns": run.turns,
				"agent_rounds": run.rounds,
				"stage_seconds": frappe.as_json(self.stage_times()),
				"review_number": number,
			},
			("Running",),
		)
		if not completed:
			# Another job ended the review, for example as dead. Its result stands.
			db.commit()
			return
		if run.pr is not None:
			run.pr.db_set(
				{
					"review_count": number,
					"last_reviewed_sha": run.head_sha,
					"latest_review": self.review.name,
					"confidence_score": run.verdict.confidence_score,
					"risk_level": run.verdict.risk_level,
				}
			)
			frappe.enqueue(
				"guard.review.jobs.after_review",
				queue="short",
				enqueue_after_commit=True,
				review=self.review.name,
				now=bool(frappe.flags.in_test),
			)
		state.notify(str(self.review.name))
		db.commit()

	# --------------------------------------------------------------- helpers

	def stage(self, name: str) -> None:
		# Under the row lock: a cancel between the check and the write would lose its reason. A cancel
		# that waits for the lock sees the new stage, and refuses once Guard posts.
		self.check_cancel(lock=True)
		self.review.db_set("stage", name, update_modified=False)
		db.commit()
		self.stage_times()
		self.stage_name = name

	def stage_times(self) -> dict[str, float]:
		"""Add the time of the current stage so far, and start its clock again."""
		now = time.time()
		if self.stage_name:
			spent = self.stage_seconds.get(self.stage_name, 0.0) + now - self.stage_started
			self.stage_seconds[self.stage_name] = round(spent, 1)
		self.stage_started = now
		return self.stage_seconds

	def between_turns(self) -> None:
		"""Before each round: stop when a newer review or job replaced this one, else keep the usage
		and the agents, so that a new job of the review goes on from here."""
		self.check_cancel()
		self.save_usage()
		self.save_checkpoint()

	def save_usage(self) -> None:
		"""Write the tokens and turns used so far. A failed, cancelled or killed job also costs tokens."""
		if self.current is None:
			return
		usage = self.current.usage
		self.review.db_set(
			{
				"input_tokens": usage.input_tokens,
				"output_tokens": usage.output_tokens,
				"cached_tokens": usage.cached_tokens,
				"agent_turns": self.current.turns,
				"agent_rounds": self.current.rounds,
				"stage_seconds": frappe.as_json(self.stage_times()),
			},
			update_modified=False,
		)
		db.commit()

	def check_cancel(self, lock: bool = False) -> None:
		row = (
			db.values("Code Review", str(self.review.name), ("cancel_requested", "job_id", "status"), lock)
			or {}
		)
		if self.claim and row.get("job_id") != self.claim:
			raise JobReplaced
		if row.get("cancel_requested") or row.get("status") == "Cancelled":
			raise Cancelled

	def existing_findings(self) -> list[dict[str, Any]]:
		if not self.review.pull_request:
			return []
		return db.dict_rows(
			"""SELECT name, fingerprint, path, title FROM `tabReview Finding`
			WHERE pull_request = %s AND status = 'Open' AND posted = 1 AND code_review != %s""",
			(self.review.pull_request, self.review.name),
		)

	def get_fresh_lines(self, store: GitStore, last_sha: str, head: str) -> dict[str, set[int]] | None:
		"""Head lines added since the last reviewed commit. Empty when that commit is gone."""
		if self.review_number() <= 1:
			return None
		if not last_sha or not store.has_commit(last_sha):
			return {}
		return {f.path: f.added_lines() for f in parse_diff(store.diff(last_sha, head))}

	def review_number(self) -> int:
		if not self.review.pull_request:
			return 1
		return int(
			db.scalar(
				"SELECT COUNT(*) FROM `tabCode Review` WHERE pull_request = %s AND status IN ('Completed', 'Running')",
				(self.review.pull_request,),
			)
			or 1
		)

	def leave_old_check(self, adapter: "ProviderAdapter", full_name: str, old: str, head: str) -> None:
		"""The pull request moved on while this review waited. End its queued check on the old commit,
		and take the check of the new one."""
		name = str(self.review.name)
		pull_request = str(self.review.pull_request)
		if self.review.check_run_id and not state.other_review_runs(pull_request, old, name):
			text = f"Guard reviews the newer commit {head[:7]} instead."
			state.end_check(adapter, full_name, pull_request, old, text, name, str(self.review.check_run_id))
		self.review.db_set("check_run_id", state.check_of(pull_request, head), update_modified=False)

	def skip_check(self, run: ReviewRun, reason: str) -> None:
		"""Mark the check skipped, so nobody waits for a review. This also ends a check that an earlier
		attempt started before it was sent back to the queue.

		Not while another review of the commit runs: the check is shared, and that review ends it.
		"""
		if state.other_review_runs(str(self.review.pull_request), run.head_sha, str(self.review.name)):
			return
		Publisher(run).skip(reason)

	def fail(self, message: str) -> None:
		if not state.move(
			str(self.review.name),
			{"status": "Failed", "error": message[:5000], "completed_at": now_datetime()},
		):
			db.commit()  # Let go of the row lock.
			return
		state.notify(str(self.review.name))
		# Before the provider calls: a cancel must not wait for them on the row lock.
		db.commit()
		try:
			self._report_failure()
		except Exception:
			frappe.log_error(redact(message), "Guard review")
		db.commit()

	def _report_failure(self) -> None:
		if not self.review.pull_request:
			return
		pr = cast("PullRequest", frappe.get_doc("Pull Request", str(self.review.pull_request)))
		adapter = adapter_for_repository(str(pr.repository))
		if not adapter:
			return
		full_name = str(db.value("Code Repository", str(pr.repository), "full_name"))
		name = str(self.review.name)
		# The status first: a failed reaction must not leave the check in progress.
		if self.review.head_sha:
			check = str(db.value("Code Review", name, "check_run_id") or "")
			text = "Guard could not finish the review"
			set_status(
				adapter, full_name, self.review.head_sha, "error", text, links.code_review(name), check
			)
		remove_eyes(adapter, full_name, int(pr.number), name)
		adapter.add_reaction(full_name, int(pr.number), "pr", "", "confused")


def by_risk(run: ReviewRun) -> list[FileDiff]:
	"""The files under review, the riskiest first: broken and stale callers, contract changes,
	callers and changed entities from the graph, then the number of changed lines."""
	score: dict[str, int] = {}
	for entity in run.changed:
		points = 1 + len(entity.callers) + 3 * len(entity.not_updated) + 5 * len(entity.broken)
		score[entity.path] = score.get(entity.path, 0) + points + (3 if entity.contract_changed else 0)

	def changed_lines(file: FileDiff) -> int:
		return sum(1 for hunk in file.hunks for line in hunk.lines if line.kind in "+-")

	# sorted() keeps the diff order when two files tie.
	return sorted(run.files, key=lambda f: (-score.get(f.path, 0), -changed_lines(f)))


def graph_verdict(run: ReviewRun) -> Verdict:
	"""A verdict from the graph alone, when the model gave none (no gateway, or no answer)."""
	contracts = [e for e in run.changed if e.contract_changed]
	lines = [
		f"Guard reviewed {len(run.files)} file(s) with the codebase graph. The AI review did not run, so no line comments came from a model."
	]
	if run.blast and run.blast.sentence():
		lines.append(run.blast.sentence())
	for entity in contracts[:10]:
		lines.append(
			f"- `{entity.qualified_name}` changed its contract; {len(entity.not_updated)} caller(s) may break."
		)
	risk = "high" if any(e.not_updated for e in contracts) else "medium" if contracts else "low"
	score = 3 if risk == "high" else 5
	return Verdict(
		summary="\n".join(lines),
		confidence_score=score,
		confidence_reason="Graph checks only.",
		risk_level=risk,
		submitted=True,
	)


def run_review(review: str, attempt: int = 0) -> None:
	"""Background job entry point."""
	ReviewPipeline.run(review, attempt)
