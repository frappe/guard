"""Render the summary comment and inline comments as GitHub markdown."""

import re

from guard.review import links, tips
from guard.review.findings import MARKS, Finding
from guard.review.run import ReviewRun

SUMMARY_MARKER = "<!-- guard:summary -->"
DESCRIPTION_START = "<!-- guard:start -->"
DESCRIPTION_END = "<!-- guard:end -->"
ALLOWED_TAGS = {"details", "summary", "sub", "h3", "table", "thead", "tbody", "tr", "td", "th", "br"}
DIAGRAM_KEYWORDS = ("sequenceDiagram", "erDiagram", "classDiagram", "flowchart", "graph")
DIAGRAM_MAX_LINES = 80


def summary(run: ReviewRun, posted: list[Finding], review_number: int, finding_urls: dict[int, str]) -> str:
	sections = run.settings.get("sections") or {}
	parts = [SUMMARY_MARKER, "<h3>Guard Overview</h3>"]
	parts.append(
		_section(sections.get("summary"), "Summary", sanitize(run.verdict.summary) or "_No summary._")
	)
	blast = run.blast.sentence() if run.blast else ""
	if blast:
		parts.append(f"**Blast radius:** {blast}")
	score = _score_text(run)
	parts.append(
		_section(
			sections.get("confidence"),
			f"Confidence Score: {run.verdict.confidence_score}/5{_tier(run)}",
			score,
		)
	)
	table = _findings_table(posted, finding_urls)
	if table:
		parts.append(_section(sections.get("issues_table"), "Findings", table))
	files = _important_files(run)
	if files:
		parts.append(_section(sections.get("summary"), "Important Files Changed", files))
	diagram = clean_diagram(run.verdict.diagram)
	if diagram and run.verdict.diagram_type != "none":
		parts.append(_section(sections.get("diagram"), "Diagram", f"```mermaid\n{diagram}\n```"))
	outside = [f for f in posted if f.outside_diff]
	if outside and run.settings.get("comments_outside_diff", True):
		parts.append(
			_section(
				{"included": True, "collapsible": True, "default_open": False},
				"Comments Outside the Diff",
				"\n\n".join(_outside(f) for f in outside),
			)
		)
	if run.context_used:
		used = "\n".join(
			f"- [{c.get('title')}]({c.get('url')}) ({c.get('source')})" for c in run.context_used
		)
		parts.append(
			_section({"included": True, "collapsible": True, "default_open": False}, "Context Used", used)
		)
	if run.notes:
		parts.append(
			_section(
				{"included": True, "collapsible": True, "default_open": True},
				"Config Problems",
				"\n".join(f"- {n}" for n in run.notes),
			)
		)
	tip = tips.in_summary(review_number)
	if tip:
		parts.append(tip)
	if not run.settings.get("hide_footer"):
		parts.append(_footer(run, review_number))
	return "\n\n".join(p for p in parts if p)


def inline(finding: Finding, name: str, fix_with_ai: bool = True) -> str:
	lines = [
		f"<!-- guard:finding:{name} -->",
		f"{MARKS.get(finding.severity, '')} **{finding.severity}** · {finding.comment_type} · **{sanitize(finding.title)}**",
		"",
		sanitize(finding.body),
	]
	if finding.question:
		lines += ["", f"**Question:** {sanitize(finding.question)}"]
	if finding.sandbox_log:
		lines += [
			"",
			"<details><summary>Sandbox run</summary>",
			"",
			"```text",
			finding.sandbox_log[-4000:],
			"```",
			"",
			"</details>",
		]
	if finding.suggestion and not finding.outside_diff:
		lines += ["", "```suggestion", finding.suggestion, "```"]
	if fix_with_ai:
		lines += [
			"",
			"<details><summary>Prompt for AI agents</summary>",
			"",
			"```text",
			fix_prompt(finding),
			"```",
			"",
			"</details>",
		]
		lines += ["", f"[Fix with your agent]({links.finding(name)})"]
	return "\n".join(lines)


def fix_prompt(finding: Finding) -> str:
	text = (
		f"In {finding.path} at lines {finding.start_line}-{finding.end_line}: {finding.title}\n\n"
		f"{finding.body}\n"
	)
	if finding.suggestion:
		text += f"\nSuggested replacement for those lines:\n{finding.suggestion}\n"
	return text + "\nCheck the claim against the code first. Fix it only if it holds."


def description(existing: str, body: str) -> str:
	block = f"{DESCRIPTION_START}\n{body}\n{DESCRIPTION_END}"
	pattern = re.compile(re.escape(DESCRIPTION_START) + r".*?" + re.escape(DESCRIPTION_END), re.DOTALL)
	if pattern.search(existing or ""):
		return pattern.sub(lambda _: block, existing)
	return f"{(existing or '').rstrip()}\n\n{block}".strip()


def sanitize(text: str) -> str:
	"""Escape HTML except an allow-list of tags. Keep code fences untouched."""
	if not text:
		return ""
	out = []
	for index, part in enumerate(re.split(r"(```.*?```)", text, flags=re.DOTALL)):
		if index % 2:
			out.append(part)
			continue
		out.append(re.sub(r"<(/?)([a-zA-Z0-9]+)([^>]*)>", _tag, part))
	return "".join(out)


def clean_diagram(text: str) -> str:
	text = re.sub(r"^```(?:mermaid)?\s*|\s*```$", "", (text or "").strip())
	if not text.startswith(DIAGRAM_KEYWORDS):
		return ""
	lines = text.splitlines()[:DIAGRAM_MAX_LINES]
	return "\n".join(line for line in lines if "click " not in line and "<script" not in line.lower())


def _tag(match: re.Match) -> str:
	if match.group(2).lower() in ALLOWED_TAGS and not match.group(3).strip():
		return match.group(0)
	return match.group(0).replace("<", "&lt;").replace(">", "&gt;")


def _section(config: dict | None, title: str, body: str) -> str:
	config = config or {"included": True, "collapsible": False, "default_open": True}
	if not config.get("included", True):
		return ""
	if config.get("collapsible"):
		state = " open" if config.get("default_open", True) else ""
		return f"<details{state}><summary><h3>{title}</h3></summary>\n\n{body}\n\n</details>"
	return f"<h3>{title}</h3>\n\n{body}"


def _score_text(run: ReviewRun) -> str:
	lines = []
	if run.verdict.confidence_reason:
		lines.append(f"- {sanitize(run.verdict.confidence_reason)}")
	if run.verdict.risk_level:
		reason = f": {sanitize(run.verdict.risk_reason)}" if run.verdict.risk_reason else ""
		lines.append(f"- Risk: **{run.verdict.risk_level}**{reason}")
	return "\n".join(lines) or "-"


def _tier(run: ReviewRun) -> str:
	return f" ({run.tier})" if run.tier in ("plus", "apex") else ""


def _findings_table(findings: list[Finding], urls: dict[int, str]) -> str:
	rows = [f for f in findings if not f.outside_diff]
	if not rows:
		return ""
	lines = ["| Severity | File | Finding |", "| - | - | - |"]
	for finding in rows:
		title = sanitize(finding.title).replace("|", "\\|")
		url = urls.get(id(finding)) or ""
		link = f"[{title}]({url})" if url else title
		lines.append(
			f"| {MARKS.get(finding.severity, '')} {finding.severity} | `{finding.path}:{finding.start_line}` | {link} |"
		)
	return "\n".join(lines)


def _important_files(run: ReviewRun) -> str:
	files = run.verdict.important_files
	if not files:
		return ""
	lines = ["| File | Overview |", "| - | - |"]
	for item in files:
		overview = sanitize(str(item.get("why") or item.get("overview") or "")).replace("|", "\\|")
		lines.append(f"| `{item['path']}` | {overview} |")
	return "\n".join(lines)


def _outside(finding: Finding) -> str:
	return f"{MARKS.get(finding.severity, '')} **{finding.severity}** `{finding.path}:{finding.start_line}` **{sanitize(finding.title)}**\n\n{sanitize(finding.body)}"


def _footer(run: ReviewRun, review_number: int) -> str:
	commit = run.commits[-1] if run.commits else None
	message = (commit.subject[:72] if commit else run.head_sha[:7]).replace("[", "(").replace("]", ")")
	commit_url = run.adapter.commit_url(run.full_name, run.head_sha) if run.adapter else ""
	parts = [
		f"Reviews ({review_number})",
		f"Last reviewed commit: [{message}]({commit_url})"
		if commit_url
		else f"Last reviewed commit: {message}",
	]
	if run.pr is not None:
		parts.append(f"[Re-trigger]({links.retrigger(str(run.pr.name))})")
		parts.append(f"[Fix all]({links.fix_all(str(run.pr.name))})")
	return "<sub>" + " · ".join(parts) + "</sub>"
