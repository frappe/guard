"""Findings: the model, and the filter stage that runs before publishing."""

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any

from guard.git.diff import FileDiff

SEVERITIES = ("P0", "P1", "P2")
COMMENT_TYPES = ("logic", "syntax", "style")
CAPS = {1: 40, 2: 20, 3: 10}
MARKS = {"P0": "🔴", "P1": "🟠", "P2": "🟡"}


@dataclass
class Finding:
	path: str
	start_line: int
	end_line: int
	severity: str
	comment_type: str
	title: str
	body: str
	category: str = "correctness"
	suggestion: str = ""
	question: str = ""
	rule_id: str | None = None
	context_name: str | None = None
	confidence: float = 0.7
	side: str = "RIGHT"
	outside_diff: bool = False
	verified: bool = False
	status: str = "Open"
	status_reason: str = ""
	sandbox_log: str = ""
	extra: dict[str, Any] = field(default_factory=dict)

	@property
	def is_security(self) -> bool:
		return self.category == "security"

	@property
	def fingerprint(self) -> str:
		title = " ".join(sorted(set(re.findall(r"[a-z0-9]+", self.title.lower()))))
		return hashlib.blake2b(f"{self.path}|{title}".encode(), digest_size=10).hexdigest()

	@property
	def kept(self) -> bool:
		return self.status == "Open"

	def drop(self, status: str, reason: str) -> None:
		self.status, self.status_reason = status, reason

	@classmethod
	def from_arguments(cls, args: dict[str, Any]) -> "Finding":
		start = _int(args.get("start_line"), 1)
		end = max(_int(args.get("end_line"), start), start)
		severity = str(args.get("severity") or "P2").upper()
		comment_type = str(args.get("comment_type") or "logic").lower()
		return cls(
			path=str(args.get("path") or "").strip().lstrip("/"),
			start_line=start,
			end_line=end,
			severity=severity if severity in SEVERITIES else "P2",
			comment_type=comment_type if comment_type in COMMENT_TYPES else "logic",
			category=str(args.get("category") or "correctness").lower(),
			title=str(args.get("title") or "").strip()[:100] or "Possible issue",
			body=str(args.get("body") or "").strip(),
			suggestion=str(args.get("suggestion") or "").strip("\n"),
			question=str(args.get("question") or "").strip(),
			rule_id=args.get("rule_id") or None,
			confidence=min(max(_float(args.get("confidence"), 0.7), 0.0), 1.0),
		)


class FindingFilter:
	"""Anchor, filter by type and strictness, dedupe, and cap. Dropped findings keep a reason."""

	def __init__(
		self,
		files: list[FileDiff],
		settings: dict[str, Any],
		existing: list[dict[str, Any]] | None = None,
		rereview: bool = False,
	):
		self.files = {f.path: f for f in files}
		self.settings = settings
		self.existing = existing or []
		self.rereview = rereview
		# Names of earlier findings that this review raised again: they still apply.
		self.reraised: set[str] = set()

	def run(self, findings: list[Finding]) -> list[Finding]:
		for finding in findings:
			self.anchor(finding)
		self.filter_types(findings)
		self.filter_strictness(findings)
		self.filter_rereview(findings)
		self.dedupe(findings)
		self.dedupe_existing(findings)
		self.cap(findings)
		return findings

	def anchor(self, finding: Finding) -> None:
		diff = self.files.get(finding.path)
		if not diff:
			finding.outside_diff = True
			return
		lines = diff.commentable_lines()
		if finding.end_line in lines:
			if finding.start_line not in lines or not _same_hunk(diff, finding.start_line, finding.end_line):
				finding.start_line = finding.end_line
			return
		near = [n for n in lines if abs(n - finding.end_line) <= 3]
		if near:
			finding.start_line = finding.end_line = min(near, key=lambda n: abs(n - finding.end_line))
			return
		deleted = {line.old_no for h in diff.hunks for line in h.lines if line.kind == "-" and line.old_no}
		near_old = [n for n in deleted if abs(n - finding.end_line) <= 3]
		if near_old:
			# The finding is about removed code: comment on the base side.
			finding.side = "LEFT"
			finding.start_line = finding.end_line = min(near_old, key=lambda n: abs(n - finding.end_line))
			finding.suggestion = ""
			return
		finding.outside_diff = True

	def filter_types(self, findings: list[Finding]) -> None:
		allowed = set(self.settings.get("comment_types") or COMMENT_TYPES)
		for finding in findings:
			if finding.kept and finding.comment_type not in allowed:
				finding.drop("Dismissed", f"comment type `{finding.comment_type}` is off")

	def filter_strictness(self, findings: list[Finding]) -> None:
		strictness = int(self.settings.get("strictness") or 2)
		for finding in findings:
			if not finding.kept:
				continue
			if strictness == 3 and finding.severity == "P2":
				finding.drop("Dismissed", "strictness 3 posts only P0 and P1")
			elif strictness == 2 and finding.severity == "P2" and finding.confidence < 0.6:
				finding.drop("Dismissed", "low-confidence P2 at strictness 2")

	def filter_rereview(self, findings: list[Finding]) -> None:
		"""A re-review posts only P0 and P1, so each push does not bring new nitpicks."""
		if not self.rereview:
			return
		for finding in findings:
			if finding.kept and finding.severity == "P2":
				finding.drop("Dismissed", "re-reviews post only P0 and P1")

	def dedupe(self, findings: list[Finding]) -> None:
		kept = sorted((f for f in findings if f.kept), key=_rank)
		for index, finding in enumerate(kept):
			for other in kept[:index]:
				if (
					other.kept
					and other.path == finding.path
					and _overlap(other, finding)
					and _similar(other.title, finding.title)
				):
					finding.drop("Dismissed", "duplicate of another finding in this review")
					break

	def dedupe_existing(self, findings: list[Finding]) -> None:
		for finding in findings:
			if not finding.kept:
				continue
			for row in self.existing:
				if row.get("fingerprint") == finding.fingerprint or (
					row.get("path") == finding.path and _similar(row.get("title") or "", finding.title, 0.6)
				):
					finding.drop("Dismissed", "already reported on this pull request")
					self.reraised.add(str(row.get("name")))
					break

	def cap(self, findings: list[Finding]) -> None:
		limit = CAPS.get(int(self.settings.get("strictness") or 2), 20)
		inline = sorted((f for f in findings if f.kept and not f.outside_diff), key=_rank)
		for finding in inline[limit:]:
			finding.drop("Dismissed", f"over the limit of {limit} comments")


def _rank(finding: Finding) -> tuple[int, float]:
	return SEVERITIES.index(finding.severity), -finding.confidence


def _overlap(a: Finding, b: Finding) -> bool:
	return a.start_line <= b.end_line + 2 and b.start_line <= a.end_line + 2


def _similar(a: str, b: str, threshold: float = 0.5) -> bool:
	left, right = set(re.findall(r"[a-z0-9]+", a.lower())), set(re.findall(r"[a-z0-9]+", b.lower()))
	if not left or not right:
		return False
	return len(left & right) / len(left | right) >= threshold


def _same_hunk(diff: FileDiff, start: int, end: int) -> bool:
	return any(h.new_start <= start and end <= h.new_end for h in diff.hunks)


def _int(value: Any, default: int) -> int:
	try:
		return int(value)
	except TypeError, ValueError:
		return default


def _float(value: Any, default: float) -> float:
	try:
		return float(value)
	except TypeError, ValueError:
		return default


def score_cap(findings: list[Finding]) -> int:
	"""The highest confidence score the posted findings allow."""
	posted = [f for f in findings if f.kept]
	p0 = sum(1 for f in posted if f.severity == "P0")
	if p0 >= 2:
		return 1
	if p0 == 1:
		return 2
	if any(f.severity == "P1" for f in posted):
		return 3
	if posted:
		return 4
	return 5
