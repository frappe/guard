"""Local reviews: the coding agent of a developer reviews a change before it is pushed, and pays for
the model. Guard applies the diff in its mirror, runs its checks and the code graph, and gives the
agent the rules and the context. Guard runs no model for it. See docs/spec/05-review-engine.md
(Local reviews)."""

import dataclasses
import re
import shutil
import time
from pathlib import Path
from typing import Any

import frappe
from frappe import _
from frappe.utils import get_url, now_datetime, time_diff_in_seconds

from guard.config.resolver import ConfigResolver
from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.git.store import GitError, GitStore
from guard.graph.overlay import PullRequestGraph
from guard.graph.queries import CodeGraph
from guard.indexing import frappe_plugin
from guard.indexing.indexer import IndexFailed
from guard.indexing.jobs import ensure_fresh
from guard.knowledge import store as kb_store
from guard.learning import memories, suppression
from guard.review import checks, context, crossrepo, links, rule_files, task
from guard.review.findings import Finding, FindingFilter, score_cap
from guard.review.pipeline import deterministic_findings
from guard.review.prompts import data, local_prompt
from guard.review.publish import Publisher
from guard.review.run import ReviewRun, Verdict
from guard.utils import cache, db
from guard.utils.redact import redact

MAX_DIFF_BYTES = 5_000_000
MAX_FILES = 300  # as many as a payload review
MAX_FINDINGS = 100  # findings that one submit can send
# How long a started review waits for its findings. An agent session is rarely longer.
SESSION_TTL = 6 * 60 * 60
MAX_GUIDE_CHARS = 80_000  # the agent reads the guide once, so it can be long
MAX_KNOWLEDGE_CHARS = 3000
MAX_IMPACTED = 20
MAX_SYMBOLS = 5
MAX_BRANCH = 140  # the length of a Data field
MAX_INSTRUCTIONS = 4000
MAX_BODY = 4000  # a finding body is at most 3 sentences; this only stops abuse
MAX_SUGGESTION = 20_000
SHA = re.compile(r"[0-9a-f]{7,64}")

FINDING_FIELDS = """Each finding is an object with these keys:
- path (required): the file, relative to the repository root.
- start_line, end_line (required): line numbers in the working tree file.
- severity (required): P0, P1 or P2.
- comment_type (required): logic, syntax or style.
- title (required): at most 12 words.
- body (required): at most 3 sentences, with the evidence.
- category: correctness, security, performance, style or test-gap.
- suggestion: replacement code for lines start_line to end_line, only when the fix is local.
- rule_id: the rule or memory that the finding acts on.
- confidence: 0 to 1, how likely the finding is true."""

SUBMIT = (
	"""How to finish:
1. Show the findings to the developer: severity, path:line, title and body.
2. Send the findings to Guard once, also when you found nothing. Guard keeps them with the
   findings of its checks, so the team sees what local reviews catch. Guard posts nothing to the
   code provider. Write the arguments to a JSON file, then run:
   frappectl api method/guard.api.local.submit --input <file>
   Or use the MCP tool `call_method` with method `guard.api.local.submit`.
   Arguments: {{"review_id": "{review_id}", "findings": [...], "summary": "...",
   "confidence_score": 0-5, "confidence_reason": "...", "risk_level": "low|medium|high|critical",
   "risk_reason": "..."}}
3. Tell the developer the link in the answer of `submit`.

"""
	+ FINDING_FIELDS
)

GUIDE = """# Guard local review

Guard reviews a change before you push it. You are the reviewer: Guard runs no model for a local
review. Guard gives you the team rules, its code graph and the findings of its checks.

1. Find the repository and the base. `full_name` is `owner/name` of the `origin` remote. The base
   is the merge base with the default branch:
   git fetch origin <default branch>
   base=$(git merge-base HEAD origin/<default branch>)
2. Include new files: `git add -N <file>` for each untracked file that belongs to the change.
3. Start the review. Send the diff from the shell, not as a tool argument, so it costs no tokens:
   git diff --binary "$base" | jq -Rs --arg full_name "<owner/name>" --arg base "$base" \\
     --arg branch "$(git branch --show-current)" --arg head "$(git rev-parse HEAD)" \\
     '{{full_name: $full_name, base: $base, branch: $branch, head: $head, diff: .}}' \\
     | frappectl api method/guard.api.local.start --input -
   Add `"instructions": "..."` to ask for a focus. Use the frappectl profile of {site}.
   `jq -Rs` reads text as UTF-8. A diff of a text file in another encoding does not apply.
4. The answer has `review_id`, `findings` of Guard's checks and `guide`. Follow `guide`: it has
   the rubric, the rules and the graph facts for this change.
5. To see the callers and callees of a symbol on the default branch, call
   `guard.api.local.symbol` with `full_name` and `name`.
6. Finish as `guide` says: show the findings, then call `guard.api.local.submit`.

Without frappectl, call `guard.api.local.start` with the MCP tool `call_method`. Then the diff is a
tool argument: do this only for a small diff.

Do not push for the developer. A local review does not replace the review of the pull request."""


def guide() -> str:
	return GUIDE.format(site=get_url())


def repository_for(full_name: str) -> str:
	"""The Code Repository of `owner/name`, or of a remote URL, that the user can read."""
	name = "/".join(re.split(r"[/:]", re.sub(r"\.git$", "", full_name.strip().rstrip("/")))[-2:])
	for repository in frappe.get_all("Code Repository", filters={"full_name": name}, pluck="name"):
		if frappe.has_permission("Code Repository", "read", str(repository)):
			return str(repository)
	frappe.throw(_("Guard has no repository {0} that you can read.").format(name), frappe.PermissionError)
	raise AssertionError  # frappe.throw raises


def start(
	full_name: str, base: str, diff: str, branch: str = "", head: str = "", instructions: str = ""
) -> dict[str, Any]:
	"""Apply the diff to `base`, run the checks and the graph, and keep the review for `submit`."""
	if len(diff.encode()) > MAX_DIFF_BYTES:
		frappe.throw(_("The diff is larger than {0} MB.").format(MAX_DIFF_BYTES // 1_000_000))
	branch, instructions = branch.strip()[:MAX_BRANCH], instructions.strip()[:MAX_INSTRUCTIONS]
	repository = repository_for(full_name)
	base = _commit(store_for(repository), base)
	review_id = frappe.generate_hash(length=16)
	try:
		return _start(review_id, repository, base, diff, branch, head, instructions)
	except BaseException:
		shutil.rmtree(_objects(review_id), ignore_errors=True)
		raise


def _start(
	review_id: str, repository: str, base: str, diff: str, branch: str, head: str, instructions: str
) -> dict[str, Any]:
	store = store_for(repository).scratch(_objects(review_id))
	try:
		tree = store.apply(base, diff if diff.endswith("\n") else diff + "\n")
	except GitError as error:
		frappe.throw(_("The diff does not apply to {0}: {1}").format(base[:12], redact(str(error))[:500]))
		raise
	# The diff of the mirror, not the one sent: it has the same form as on a pull request.
	diff_text = store.diff(base, tree)
	run = _run(repository, store, base, tree, diff_text, branch)
	if not run.files:
		frappe.throw(_("The diff has no files to review: they are ignored, binary or empty."))
	index = _index(run)
	run.graph = CodeGraph(index) if index else None
	overlay = PullRequestGraph(run.graph, run.head, run.base, run.files, run.frappe_app)
	run.changed = overlay.build()
	provider = db.value("Code Repository", repository, "code_provider")
	others = crossrepo.linked(repository, str(provider or ""), run.cfg.repos)
	crossrepo.attach(run.changed, others, index)
	run.blast = overlay.blast_radius(run.changed)
	if run.settings.get("include_rule_files", True):
		# From the base, as on a pull request: the change cannot change its own rules.
		run.rule_files = rule_files.collect(
			run.paths, lambda path: store.show(base, path), lambda path: store.list_dir(base, path)
		)
	found = deterministic_findings(run, others, index)
	FindingFilter(run.files, run.settings).run(found)
	found = [f for f in found if f.kept]
	cache.set(
		_key(review_id),
		{
			"user": frappe.session.user,
			"repository": repository,
			"base": base,
			"tree": tree,
			"head": head if SHA.fullmatch(head or "") else "",
			"branch": branch,
			"instructions": instructions,
			"checks": [dataclasses.asdict(f) for f in found],
			"started_at": now_datetime(),
		},
		SESSION_TTL,
	)
	return {
		"review_id": review_id,
		"repository": run.full_name,
		"base": base,
		"tree": tree,
		"files": len(run.files),
		"findings": [_finding(f) for f in found],
		"guide": guide_for(run, found, review_id, instructions),
	}


def guide_for(run: ReviewRun, found: list[Finding], review_id: str, instructions: str = "") -> str:
	"""What the agent needs to review this change: the rubric, the rules and the graph facts."""
	parts = [local_prompt(int(run.settings.get("strictness") or 2))]
	parts.append(f"Review id: {review_id}. Repository: {run.full_name}. Base: {run.base_sha}.")
	if instructions:
		parts.append("Extra instructions from the developer:\n" + data("request", instructions))
	for source, text in context.instructions(run.cfg)[:10]:
		parts.append(f"Team instructions from {source}:\n" + data("instructions", text))
	parts.append(task.team_rules(run))
	parts.append(task.project_rules(run))
	if run.frappe_app:
		parts.append(checks.FRAPPE_RULES)
	team = db.value("Code Repository", run.repository, "team") if run.repository else None
	notes = [memories.note(m) for m in memories.for_paths(run.full_name, team, run.paths)]
	if notes:
		parts.append(
			"Team memories. Follow them, and give the rule_id of a memory that a finding acts on:\n"
			+ data("memories", "\n".join(f"- {note}" for note in notes))
		)
	for document in kb_store.documents_for_paths(str(run.repository), run.paths):
		content = str(document.get("content") or "")[:MAX_KNOWLEDGE_CHARS]
		parts.append(f"Knowledge base page {document['path']}:\n" + data("knowledge", content))
	parts += _graph(run)
	noisy = sorted(suppression.noisy_types(str(run.repository)))
	if noisy:
		parts.append(
			f"The team rarely acts on {', '.join(noisy)} comments. Report them only when you are sure."
		)
	if found:
		lines = "\n".join(f"- {f.severity} {f.path}:{f.start_line} {f.title}" for f in found)
		parts.append("Findings of Guard's checks, already part of this review:\n" + data("checks", lines))
	if run.notes:
		parts.append("Notes:\n" + data("notes", "\n".join(f"- {note}" for note in run.notes)))
	parts.append(SUBMIT.format(review_id=review_id))
	return "\n\n".join(p for p in parts if p)[:MAX_GUIDE_CHARS]


def submit(
	review_id: str,
	findings: list[dict[str, Any]],
	summary: str = "",
	confidence_score: int | None = None,
	confidence_reason: str = "",
	risk_level: str = "",
	risk_reason: str = "",
) -> dict[str, Any]:
	"""Filter the findings of the agent with those of the checks, and save them as a Code Review."""
	with cache.exclusive(f"guard:local-submit:{review_id}", 120) as acquired:
		if not acquired:
			frappe.throw(_("Review {0} is being submitted now. Wait for that call.").format(review_id))
		session = cache.get(_key(review_id))
		if not session or session.get("user") != frappe.session.user:
			frappe.throw(_("Guard has no started review {0}. Start the review again.").format(review_id))
			raise AssertionError  # frappe.throw raises
		repository = str(session["repository"])
		frappe.has_permission("Code Repository", "read", repository, throw=True)
		store = store_for(repository).scratch(_objects(review_id))
		try:
			# The tree stays in the scratch objects, so the session does not need to hold the diff.
			diff_text = store.diff(session["base"], session["tree"])
			run = _run(repository, store, session["base"], session["tree"], diff_text, session["branch"])
		except GitError:
			frappe.throw(_("Guard lost the change of review {0}. Start the review again.").format(review_id))
			raise
		run.review = _save_review(run, session, review_id)
		run.findings = [Finding(**f) for f in session["checks"]]
		run.findings += [_from_agent(f) for f in findings[:MAX_FINDINGS] if isinstance(f, dict)]
		FindingFilter(run.files, run.settings).run(run.findings)
		for finding in run.findings:
			# A finding outside the diff can be about a caller. One on a missing file is a mistake.
			if finding.kept and finding.outside_diff and run.head(finding.path) is None:
				finding.drop("Dismissed", "the file is not in the repository")
		# No embeddings: Guard spends no tokens on a local review.
		suppression.suppress(run.findings, repository, similar=False)
		verdict = Verdict.from_arguments(
			{
				"summary": summary,
				"confidence_score": confidence_score,
				"confidence_reason": confidence_reason,
				"risk_level": risk_level,
				"risk_reason": risk_reason,
			}
		)
		verdict.confidence_score = min(verdict.confidence_score, score_cap(run.findings))
		kept = [f for f in run.findings if f.kept]
		run.review.db_set(
			{
				"summary": verdict.summary,
				"confidence_score": verdict.confidence_score,
				"confidence_reason": verdict.confidence_reason,
				"risk_level": verdict.risk_level,
				"risk_reason": verdict.risk_reason,
				"findings_count": len(kept),
				"suppressed_count": len(run.findings) - len(kept),
			}
		)
		Publisher(run).save_findings()
		cache.delete(_key(review_id))
		shutil.rmtree(_objects(review_id), ignore_errors=True)
	return {
		"review": run.review.name,
		"url": links.code_review(str(run.review.name)),
		"confidence_score": verdict.confidence_score,
		"kept": [_finding(f) for f in kept],
		"dropped": [
			{**_finding(f), "reason": f.status_reason or f.status} for f in run.findings if not f.kept
		],
	}


def symbol(full_name: str, name: str) -> dict[str, Any]:
	"""Definitions of a symbol on the default branch, with their callers and callees."""
	repository = repository_for(full_name)
	try:
		graph = CodeGraph(ensure_fresh(repository, refresh=False))
	except IndexFailed:
		frappe.throw(_("{0} has no code graph yet.").format(full_name))
		raise
	return {
		"definitions": [
			{
				"label": e.label,
				"kind": e.kind,
				"signature": e.signature,
				"path": e.path,
				"start_line": e.start_line,
				"end_line": e.end_line,
				"callers": [
					{
						"path": c.path,
						"line": c.line,
						"caller": c.entity.label if c.entity else c.name,
						"resolved": c.resolved,
					}
					for c in graph.callers(e)
				],
				"callees": [
					{"name": c.name, "target": c.entity.label if c.entity else None, "resolved": c.resolved}
					for c in graph.callees(e)
				],
			}
			for e in graph.definitions(name, limit=MAX_SYMBOLS)
		]
	}


def _commit(store: GitStore, base: str) -> str:
	"""`base` as a full sha. Fetch it when the mirror does not have it yet."""
	base = base.strip().lower()
	if not SHA.fullmatch(base):
		frappe.throw(_("The base must be a commit sha: `git merge-base HEAD origin/<default branch>`."))
	try:
		store.fetch_sha(base)
		return store.rev_parse(base)
	except GitError:
		frappe.throw(
			_("Guard cannot find commit {0}. Use the merge base with the default branch.").format(base)
		)
		raise


def _run(repository: str, store: GitStore, base: str, tree: str, diff_text: str, branch: str) -> ReviewRun:
	files = parse_diff(diff_text)
	# Config from the base, as on a pull request.
	config = ConfigResolver.from_store(repository, store, base).resolve_pr([f.path for f in files])
	reviewed = set(config.reviewed_paths())
	run = ReviewRun(
		review=None,
		repository=repository,
		full_name=str(db.value("Code Repository", repository, "full_name")),
		head=lambda path: store.show(tree, path),
		base=lambda path: store.show(base, path),
		list_dir=lambda path: store.list_dir(tree, path),
		head_sha=tree,
		base_sha=base,
		config_sha=base,
		title=f"Local change on {branch}" if branch else "Local change",
		author=str(frappe.session.user),
		head_branch=branch,
		base_branch=str(db.value("Code Repository", repository, "default_branch") or ""),
		all_files=files,
		files=[f for f in files if f.path in reviewed and not f.is_binary][:MAX_FILES],
		config=config,
		frappe_app=frappe_plugin.is_frappe_app({e.path for e in store.ls_tree(tree)}),
	)
	run.notes = list(config.errors)
	return run


def _index(run: ReviewRun) -> str | None:
	"""The last complete index. A local review does not wait for indexing."""
	try:
		return ensure_fresh(str(run.repository), refresh=False)
	except IndexFailed:
		run.notes.append("Guard has no code graph of this repository yet. Find the callers with grep.")
		return None


def _graph(run: ReviewRun) -> list[str]:
	parts = []
	if run.blast and run.blast.sentence():
		parts.append(f"Blast radius from the graph: {run.blast.sentence()}")
		if run.blast.untested:
			parts.append("Changed public functions with no test path: " + ", ".join(run.blast.untested[:10]))
	changed = task.changed_entities(run)
	if changed:
		parts.append("Changed entities and call sites at risk:\n" + data("graph", changed))
	impacted = list(context.impacted_files(run.changed).items())[:MAX_IMPACTED]
	if impacted:
		lines = "\n".join(f"- {path}: {', '.join(reasons[:3])}" for path, reasons in impacted)
		parts.append("Files outside the change that use the changed code. Read the calls:\n" + lines)
	return parts


def _save_review(run: ReviewRun, session: dict[str, Any], review_id: str) -> Any:
	started = session["started_at"]
	return frappe.get_doc(
		{
			"doctype": "Code Review",
			"repository": run.repository,
			"source": "local",
			"trigger": "local",
			"status": "Completed",
			"stage": "after",
			"requested_by": frappe.session.user,
			"triggered_by_user": frappe.session.user,
			"strictness": run.settings.get("strictness"),
			# A commit, when the agent sent one. The reviewed tree is not a commit, so it is not kept.
			"head_sha": session["head"],
			"base_sha": run.base_sha,
			"merge_base_sha": run.base_sha,
			"branch": run.head_branch,
			"instructions": session["instructions"],
			"files_reviewed": len(run.files),
			"credits": 0,
			"started_at": started,
			"completed_at": now_datetime(),
			"duration": time_diff_in_seconds(now_datetime(), started),
			"job_id": review_id,
			"changed_files": frappe.as_json([{"path": f.path, "status": f.status} for f in run.all_files]),
		}
	).insert(ignore_permissions=True)


def _from_agent(args: dict[str, Any]) -> Finding:
	finding = Finding.from_arguments(args)
	finding.rule_id = str(finding.rule_id)[:140] if isinstance(finding.rule_id, str | int) else None
	finding.body = finding.body[:MAX_BODY]
	finding.suggestion = finding.suggestion[:MAX_SUGGESTION]
	return finding


def _finding(finding: Finding) -> dict[str, Any]:
	return {
		"path": finding.path,
		"start_line": finding.start_line,
		"end_line": finding.end_line,
		"severity": finding.severity,
		"comment_type": finding.comment_type,
		"title": finding.title,
		"body": finding.body,
		"rule_id": finding.rule_id,
	}


def _key(review_id: str) -> str:
	return f"guard:local-review:{review_id}"


def _objects(review_id: str) -> Path:
	"""The git objects of one local review: the patched blobs and trees."""
	return Path(frappe.get_site_path("private", "guard", "local", review_id))


def remove_abandoned() -> None:
	"""Every hour: delete the objects of local reviews that nobody submitted. Their session expired."""
	root = _objects("x").parent
	if not root.is_dir():
		return
	for directory in root.iterdir():
		if directory.stat().st_mtime < time.time() - SESSION_TTL:
			shutil.rmtree(directory, ignore_errors=True)
