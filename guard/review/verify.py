"""Second pass that removes false positives (plus and apex; P0 only for base), and the fact
check of the confidence score."""

import re
from typing import Any

from guard.llm.client import LLMClient
from guard.review.findings import SEVERITIES, Finding, score_cap, similar_titles
from guard.review.prompts import VERDICT, VERIFIER, data
from guard.review.rule_files import mandatory_rules
from guard.review.run import ReviewRun
from guard.utils import db

CODE_RADIUS = 15
# `path/to/file.py:45`, `file.py:10-20` or `file.py:45,50` in the text of a finding.
CITED = re.compile(r"([\w][\w./-]*\.\w{1,6}):(\d+)(?:[-\u2013](\d+))?")
MAX_CITED = 4
CITED_RADIUS = 8
LIBRARY_CONFIDENCE = 0.5


def verify(run: ReviewRun) -> None:
	candidates = [f for f in run.findings if f.kept and (run.tier != "base" or f.severity == "P0")]
	if not candidates:
		return
	client = LLMClient(tier=run.tier, purpose="verify", usage=run.usage)
	if not client.online:
		return
	rules = {rule.id: rule.text for rule in mandatory_rules(run.rule_files)}
	earlier = _earlier_verdicts(run)
	prompt = "\n\n".join(
		_item(run, index, finding, rules.get(finding.rule_id or "", ""))
		+ _cited(run, finding)
		+ _earlier(finding, earlier, index)
		for index, finding in enumerate(candidates)
	)
	answers = client.complete_json(VERIFIER, prompt, default=[])
	if not isinstance(answers, list):
		return
	for answer in answers:
		if not isinstance(answer, dict):
			continue
		index = answer.get("index")
		if not isinstance(index, int) or not 0 <= index < len(candidates):
			continue
		finding = candidates[index]
		finding.verified = True
		if answer.get("valid") is False:
			finding.drop("Dismissed", f"verifier: {str(answer.get('reason') or 'not valid')[:200]}")
			continue
		proven = finding.severity == "P0" and proven_break(run, finding)
		if answer.get("severity") in SEVERITIES and not proven:
			finding.severity = answer["severity"]
		if isinstance(answer.get("confidence"), int | float):
			finding.confidence = min(max(float(answer["confidence"]), 0.0), 1.0)
		if answer.get("evidence") == "library" and not proven:
			# The claim rests on how a library behaves, which no code here shows. Models get that
			# wrong: they posted P1s about boto3 and frappe.log_error that were not true.
			finding.severity = "P2"
			finding.confidence = min(finding.confidence, LIBRARY_CONFIDENCE)
			finding.extra["evidence"] = "library"


def check_verdict(run: ReviewRun) -> None:
	"""Fact-check the score and its reason, when they can rest on more than the posted findings.

	The reviewer scores from what it suspects, before its findings are checked. When the check
	refutes some, the score can rest on issues that are wrong. When findings are dropped, the
	reason can name issues that the pull request never sees. A finding that is held back, for
	example as minor, still counts: nothing showed that it is wrong. The reviewer does not see
	the open findings of earlier reviews, so the check counts the ones that still apply. Without
	a model the score and reason stay as they are.
	"""
	verdict = run.verdict
	if not verdict.submitted:
		return
	dropped = any(not f.kept for f in run.findings)
	cap = score_cap(run.findings, [row["severity"] for row in run.earlier_open])
	if verdict.confidence_score >= cap and not dropped and not run.earlier_open:
		return
	client = LLMClient(tier=run.tier, purpose="verdict", usage=run.usage)
	if not client.online:
		return
	claim = (
		f"Score: {verdict.confidence_score}\nReason: {verdict.confidence_reason}\nSummary:\n{verdict.summary}"
	)
	parts = [data("review", claim)]
	parts += [_checked(run, index, finding) for index, finding in enumerate(run.findings)]
	if not run.findings:
		parts.append("The reviewer reported no findings.")
	for offset, row in enumerate(run.earlier_open, len(run.findings)):
		line = row["start_line"] or 1  # an Int column
		earlier = Finding(
			str(row["path"]),
			line,
			line,
			str(row["severity"]),
			"logic",
			str(row["title"]),
			str(row["body"] or ""),
		)
		parts.append(
			_item(run, offset, earlier)
			+ "\n"
			+ data(f"what happened to finding {offset}", "open from an earlier review: it still applies")
		)
	answer = client.complete_json(VERDICT, "\n\n".join(parts), default={})
	score = answer.get("score") if isinstance(answer, dict) else None
	if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 5:
		return
	verdict.confidence_score = score
	if reason := str(answer.get("reason") or "").strip():
		verdict.confidence_reason = reason[:500]


def _checked(run: ReviewRun, index: int, finding: Finding) -> str:
	if finding.kept:
		fate = "posted"
	elif finding.status_reason.startswith("verifier:"):
		fate = f"refuted: {finding.status_reason.removeprefix('verifier:').strip()}"
	else:
		fate = f"held back: {finding.status_reason}"
	return _item(run, index, finding) + "\n" + data(f"what happened to finding {index}", fate)


def proven_break(run: ReviewRun, finding: Finding) -> bool:
	"""The graph shows that the code at the finding breaks a caller, so it stays P0."""
	for entity in run.changed:
		external = [caller for caller in entity.external if caller.breaks]
		if not entity.broken and not external:
			continue
		start, end = entity.head_start, entity.head_end
		if entity.status == "deleted" and entity.base:
			# A deleted entity has no head lines: compare with its lines on the base side.
			start, end = entity.base.start_line, entity.base.end_line
		if (
			finding.path == entity.path
			and start <= finding.end_line
			and finding.start_line <= max(end, start)
		):
			return True
		if any(
			link.path == finding.path and abs(link.line - finding.start_line) <= 2 for link in entity.broken
		):
			return True
	return False


def _cited(run: ReviewRun, finding: Finding) -> str:
	"""The code at the places that the finding names in other files, or elsewhere in its file.

	The evidence of a finding is often in another file: an old patch that still uses a renamed
	DocType. Without it the verifier sees unrelated code and dismisses a real issue.
	"""
	blocks: list[str] = []
	seen: set[tuple[str, int]] = set()
	for match in CITED.finditer(f"{finding.title}\n{finding.body}"):
		path, start = match.group(1).lstrip("./"), int(match.group(2))
		end = int(match.group(3) or start)
		near = path == finding.path and abs(start - finding.start_line) <= CODE_RADIUS
		if near or (path, start) in seen:
			continue
		seen.add((path, start))
		lines = (run.head(path) or "").splitlines()
		if not lines:
			continue
		first, last = max(start - CITED_RADIUS, 1), min(max(end, start) + CITED_RADIUS, len(lines))
		code = "\n".join(f"{n:5} {lines[n - 1]}" for n in range(first, last + 1))
		blocks.append(data(f"cited code {path}", code))
		if len(blocks) >= MAX_CITED:
			break
	return "".join(f"\n{block}" for block in blocks)


def _earlier_verdicts(run: ReviewRun) -> list[dict[str, Any]]:
	"""What the verifier said about findings of earlier reviews of the pull request."""
	if not run.review or not run.review.pull_request:
		return []
	return db.dict_rows(
		"""SELECT path, title, status_reason FROM `tabReview Finding`
		WHERE pull_request = %s AND code_review != %s AND status_reason LIKE 'verifier:%%'""",
		(run.review.pull_request, run.review.name),
	)


def _earlier(finding: Finding, rows: list[dict[str, Any]], index: int) -> str:
	"""An earlier verdict on a similar finding, so two reviews do not decide the same claim both ways."""
	row = next(
		(r for r in rows if r["path"] == finding.path and similar_titles(r["title"] or "", finding.title)),
		None,
	)
	if row is None:
		return ""
	text = f"{row['title']}\nDismissed then because: {str(row['status_reason']).removeprefix('verifier:').strip()}"
	return "\n" + data(f"earlier verdict on a similar finding to {index}", text)


def _item(run: ReviewRun, index: int, finding: Finding, rule: str = "") -> str:
	source = run.head(finding.path) or ""
	lines = source.splitlines()
	start = max(finding.start_line - CODE_RADIUS, 1)
	end = min(finding.end_line + CODE_RADIUS, len(lines))
	code = "\n".join(f"{n:5} {lines[n - 1]}" for n in range(start, end + 1)) if lines else ""
	claim = f"[{index}] {finding.severity} {finding.comment_type} {finding.path}:{finding.start_line}-{finding.end_line}\n{finding.title}\n{finding.body}"
	block = data(f"finding {index}", claim) + "\n" + data(f"code {finding.path}", code)
	return block + "\n" + data(f"mandatory project rule {finding.rule_id}", rule) if rule else block
