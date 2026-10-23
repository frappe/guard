"""The user message of a review: the pull request, the diff, and the graph context."""

from guard.review import checks, rule_files
from guard.review.context import BuiltContext, excerpt
from guard.review.prompts import TASK, data
from guard.review.run import ReviewRun
from guard.review.spans import ContextSpan

DIFF_LIMIT = 120_000
# The agent sends this message again on every turn, so each token here costs one per turn.
# The model reads more with its tools when it needs it.
BUDGET_TOKENS = {"base": 12_000, "plus": 20_000, "apex": 32_000}
CHARS_PER_TOKEN = 4
# Lines of a changed file shown around each hunk. The diff itself has 3.
HUNK_PAD = 25
# Lower number = more important. Files beyond the budget are left out, least important first.
PRIORITY = (
	("project rule file", 1),
	("custom instruction", 1),
	("changed file", 1),
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
	diff = checks.mask_secrets("\n\n".join(f.annotated() for f in run.files))[:DIFF_LIMIT]
	used = sum(len(p) for p in parts) + len(diff) + len(extra)
	budget = BUDGET_TOKENS.get(run.tier, BUDGET_TOKENS["base"]) * CHARS_PER_TOKEN - used
	parts += context_files(built, budget, around_hunks(run))
	parts.append("Annotated diff:\n" + data("diff", diff))
	if extra:
		parts.append(extra)
	return "\n\n".join(p for p in parts if p)


def context_files(
	built: BuiltContext, budget_chars: int, around: dict[str, list[ContextSpan] | None]
) -> list[str]:
	"""Context files that fit the budget, best first: spans for files outside the diff, and the
	lines around the hunks for the changed files in `around`. Other changed files are left out:
	an added file is all in the diff, and a file of another diff part is not reviewed here."""
	blocks = []
	for file in built.pack.files:
		if file.reason.startswith("changed file"):
			spans = around.get(file.path)
			if not spans:
				continue
		else:
			spans = built.spans.get(file.path)
		text = checks.mask_secrets(excerpt(file.content, spans))
		blocks.append((_priority(file.reason), data(f"{file.path} ({file.reason[:200]})", text)))
	chosen, used = [], 0
	for priority, block in sorted(blocks, key=lambda b: b[0]):
		if used + len(block) > budget_chars and priority > 1:
			continue
		chosen.append(block)
		used += len(block)
	return chosen


def around_hunks(run: ReviewRun) -> dict[str, list[ContextSpan] | None]:
	"""Spans around the hunks of each changed file, or None for an added file."""
	return {
		f.path: None
		if f.status == "added"
		else [ContextSpan(max(h.new_start - HUNK_PAD, 1), h.new_end + HUNK_PAD, "changed") for h in f.hunks]
		for f in run.files
	}


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
	rules = rule_files.mandatory_rules(run.rule_files)[:40]
	if not rules:
		return ""
	lines = "\n".join(f"- {rule.text} (rule_id: {rule.id})" for rule in rules)
	return "Mandatory project rules. A finding that breaks one is at least P1:\n" + data(
		"project rules", lines
	)


def _changed(run: ReviewRun) -> str:
	blocks = []
	for entity in run.changed[:25]:
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
