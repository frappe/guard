"""Second pass that removes false positives (plus and apex; P0 only for base)."""

from guard.llm.client import LLMClient
from guard.review.findings import SEVERITIES, Finding
from guard.review.prompts import VERIFIER, data
from guard.review.run import ReviewRun

CODE_RADIUS = 15


def verify(run: ReviewRun) -> None:
	candidates = [f for f in run.findings if f.kept and (run.tier != "base" or f.severity == "P0")]
	if not candidates:
		return
	client = LLMClient(tier=run.tier, purpose="verify", usage=run.usage)
	if not client.online:
		return
	prompt = "\n\n".join(_item(run, index, finding) for index, finding in enumerate(candidates))
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
		if answer.get("severity") in SEVERITIES and not (
			finding.severity == "P0" and proven_break(run, finding)
		):
			finding.severity = answer["severity"]
		if isinstance(answer.get("confidence"), int | float):
			finding.confidence = min(max(float(answer["confidence"]), 0.0), 1.0)


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


def _item(run: ReviewRun, index: int, finding: Finding) -> str:
	source = run.head(finding.path) or ""
	lines = source.splitlines()
	start = max(finding.start_line - CODE_RADIUS, 1)
	end = min(finding.end_line + CODE_RADIUS, len(lines))
	code = "\n".join(f"{n:5} {lines[n - 1]}" for n in range(start, end + 1)) if lines else ""
	claim = f"[{index}] {finding.severity} {finding.comment_type} {finding.path}:{finding.start_line}-{finding.end_line}\n{finding.title}\n{finding.body}"
	return data(f"finding {index}", claim) + "\n" + data(f"code {finding.path}", code)
