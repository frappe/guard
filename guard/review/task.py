"""The user message of a review: the pull request, the diff, and the graph context."""

from guard.review import checks, rule_files
from guard.review.context import BuiltContext, excerpt
from guard.review.prompts import DELTA, DELTA_DIAGRAM, TASK, data
from guard.review.run import ReviewRun

DIFF_LIMIT = 120_000
# The agent sends this message again on every turn, so each token here costs one per turn.
# The model reads more with its tools when it needs it.
BUDGET_TOKENS = {"base": 12_000, "plus": 20_000, "apex": 32_000}
CHARS_PER_TOKEN = 4
# Head lines shown around each hunk of the diff, past the 3 lines that git gives. A large diff
# gets fewer: the lines around the hunks take at most half of the budget.
HUNK_PADS = (22, 10)
HUNK_PAD = HUNK_PADS[0]
MAX_RULES = 80
MAX_REST = 100  # names of the files not changed since the last review
SUMMARY_LIMIT = 4000  # characters of the last summary on a re-review
DIAGRAM_LIMIT = 3000  # characters of the last diagram: a long one is not worth its tokens on each turn
# Lower number = more important. Files beyond the budget are left out, least important first.
PRIORITY = (
	("project rule file", 1),
	("custom instruction", 1),
	("caller of", 2),
	("call site of", 2),
	("impacted", 3),
	("team context file", 4),
	("similar", 5),
)


def compose(run: ReviewRun, built: BuiltContext, extra: str = "") -> str:
	parts = [TASK]
	if run.instructions:
		parts.append("Extra instructions for this review:\n" + data("request", run.instructions))
	meta = [
		f"Title: {run.title}",
		f"Author: {run.author}",
		f"Branches: {run.head_branch} -> {run.base_branch}",
	]
	if run.labels:
		meta.append("Labels: " + ", ".join(run.labels))
	if run.commits:
		meta.append("Commits:\n" + "\n".join(f"- {c.subject}" for c in run.commits[-30:]))
	parts.append("Pull request:\n" + data("pull request", "\n".join(meta) + "\n\n" + (run.description or "")))
	parts.append(_delta(run))
	parts.append(_rules(run))
	parts.append(_project_rules(run))
	if run.frappe_app:
		parts.append(checks.FRAPPE_RULES)
	if run.blast and run.blast.sentence():
		parts.append(f"Blast radius from the graph: {run.blast.sentence()}")
		if run.blast.untested:
			parts.append("Changed public functions with no test path: " + ", ".join(run.blast.untested[:10]))
	changed = _changed(run)
	if changed:
		parts.append("Changed entities and call sites at risk:\n" + data("graph", changed))
	if built.pack.questions:
		parts.append(
			"\n".join(
				["Questions from the graph to check:", *(q.as_text() for q in built.pack.questions[:30])]
			)
		)
	for note in built.pack.notes[:12]:
		parts.append(data("context", note))
	total = BUDGET_TOKENS.get(run.tier, BUDGET_TOKENS["base"]) * CHARS_PER_TOKEN
	diff = checks.mask_secrets(annotated_diff(run, total // 2))[:DIFF_LIMIT]
	used = sum(len(p) for p in parts) + len(diff) + len(extra)
	budget = total - used
	parts += context_files(built, budget)
	since = f" (changes since {run.delta.since[:7]})" if run.delta else ""
	parts.append(f"Annotated diff{since}:\n" + data("diff", diff))
	if extra:
		parts.append(extra)
	return "\n\n".join(p for p in parts if p)


def annotated_diff(run: ReviewRun, room: int = 0) -> str:
	"""The diff of the files under review, each hunk with the head lines around it.

	The lines around a hunk are in the diff and not in a context file of their own, so the model
	reads them once. An added file is all in the diff. With `room`, the lines around the hunks add
	at most that many characters, or none.
	"""
	heads = {
		file.path: (run.head(file.path) or "").splitlines()
		for file in run.files
		if file.status not in ("added", "deleted")
	}
	plain = "\n\n".join(file.annotated() for file in run.files)
	for pad in HUNK_PADS:
		diff = "\n\n".join(file.annotated(heads.get(file.path), pad) for file in run.files)
		if not room or len(diff) - len(plain) <= room:
			return diff
	return plain


def context_files(built: BuiltContext, budget_chars: int) -> list[str]:
	"""Context files that fit the budget, best first: the spans of each file."""
	blocks = []
	for file in built.pack.files:
		text = checks.mask_secrets(excerpt(file.content, built.spans.get(file.path)))
		if not text.strip():
			continue
		blocks.append((_priority(file.reason), data(f"{file.path} ({file.reason[:200]})", text)))
	chosen, used = [], 0
	for priority, block in sorted(blocks, key=lambda b: b[0]):
		if used + len(block) > budget_chars and priority > 1:
			continue
		chosen.append(block)
		used += len(block)
	return chosen


def _priority(reason: str) -> int:
	return next((rank for prefix, rank in PRIORITY if reason.startswith(prefix)), 3)


def _rules(run: ReviewRun) -> str:
	if not run.config:
		return ""
	lines: list[str] = []
	seen: set[str] = set()
	for path, config in run.config.files.items():
		for rule in config.rules:
			key = rule.id or rule.rule
			if key in seen:
				continue
			seen.add(key)
			severity = f" [{rule.severity}]" if rule.severity else ""
			ident = f" (rule_id: {rule.id})" if rule.id else ""
			lines.append(f"- {rule.rule}{severity}{ident}")
		for source, text in config.rules_md:
			if source not in seen:
				seen.add(source)
				lines.append(f"From {source}:\n{text}")
		for note in config.notes:
			if note.content not in seen:
				seen.add(note.content)
				lines.append(f"- Note for {path}: {note.content}")
	return "Team rules:\n" + data("rules", "\n".join(lines)) if lines else ""


def _project_rules(run: ReviewRun) -> str:
	"""The project rules that cover the changed files, as a checklist."""
	rules = [r for r in rule_files.project_rules(run.rule_files) if any(r.covers(p) for p in run.paths)]
	if not rules:
		return ""
	lines = "\n".join(f"- {rule.text} (rule_id: {rule.id})" for rule in rules[:MAX_RULES])
	return (
		"Project rules. Check the change against each one, and report every break as a P2 "
		"finding with its rule_id:\n" + data("project rules", lines)
	)


def _delta(run: ReviewRun) -> str:
	"""On a push re-review: what was reviewed before, and the files that did not change since."""
	if not run.delta:
		return ""
	parts = [DELTA.format(since=run.delta.since[:7])]
	if run.delta.summary:
		parts.append(
			"Summary of the last review:\n" + data("last summary", run.delta.summary[:SUMMARY_LIMIT])
		)
	if run.delta.diagram_type and run.delta.diagram and len(run.delta.diagram) <= DIAGRAM_LIMIT:
		parts.append(
			DELTA_DIAGRAM.format(kind=run.delta.diagram_type) + "\n" + data("last diagram", run.delta.diagram)
		)
	if run.delta.rest:
		names = "\n".join(f"- {name}" for name in run.delta.rest[:MAX_REST])
		parts.append("Other changed files of the pull request, not changed since:\n" + names)
	return "\n\n".join(parts)


def _changed(run: ReviewRun) -> str:
	blocks = []
	# On a re-review, only the entities of the files in the diff: the others were reviewed before.
	names = run.delta.names if run.delta else None
	entities = [e for e in run.changed if names is None or e.path in names]
	for entity in entities[:25]:
		block = [entity.label]
		if entity.contract_changed and entity.base:
			block.append(f"  before: {entity.base.signature}")
			block.append(f"  after:  {entity.head_signature}")
		if entity.callers:
			block.append(f"  callers: {len(entity.callers)}, may break: {len(entity.not_updated)}")
		for site in entity.at_risk[:6]:
			block.append(f"  at risk {site.link.path}:{site.link.line}\n{site.snippet}")
		if entity.external:
			repositories = sorted({caller.full_name for caller in entity.external})
			block.append(f"  used in other repositories: {len(entity.external)} ({', '.join(repositories)})")
		if entity.siblings:
			block.append("  similar: " + ", ".join(s.label for s in entity.siblings[:3]))
		if entity.tests:
			block.append("  tests: " + ", ".join(entity.tests[:4]))
		blocks.append("\n".join(block))
	return "\n\n".join(blocks)
