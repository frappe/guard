"""The user message of a review: the pull request, the diff, and the graph context."""

from guard.review.context import BuiltContext
from guard.review.prompts import TASK, data
from guard.review.run import ReviewRun

DIFF_LIMIT = 120_000


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
	for file in built.pack.files:
		parts.append(data(f"{file.path} ({file.reason})", file.content))
	diff = "\n\n".join(f.annotated() for f in run.files)
	parts.append("Annotated diff:\n" + data("diff", diff[:DIFF_LIMIT]))
	if extra:
		parts.append(extra)
	return "\n\n".join(p for p in parts if p)


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


def _changed(run: ReviewRun) -> str:
	blocks = []
	for entity in run.changed[:25]:
		block = [entity.label]
		if entity.contract_changed and entity.base:
			block.append(f"  before: {entity.base.signature}")
			block.append(f"  after:  {entity.head_signature}")
		if entity.callers:
			block.append(f"  callers: {len(entity.callers)}, not updated: {len(entity.not_updated)}")
		for site in entity.at_risk[:6]:
			block.append(f"  at risk {site.link.path}:{site.link.line}\n{site.snippet}")
		if entity.siblings:
			block.append("  similar: " + ", ".join(s.label for s in entity.siblings[:3]))
		if entity.tests:
			block.append("  tests: " + ", ".join(entity.tests[:4]))
		blocks.append("\n".join(block))
	return "\n\n".join(blocks)
