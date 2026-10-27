"""The state that the stages of one review share."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from guard.config.resolver import PullRequestConfig
from guard.git.diff import FileDiff
from guard.git.store import Commit
from guard.graph.overlay import BlastRadius, ChangedEntity
from guard.graph.queries import CodeGraph
from guard.llm.types import Usage
from guard.providers.base import ProviderAdapter
from guard.review.findings import Finding

Reader = Callable[[str], str | None]
Lister = Callable[[str], list[tuple[str, str]]]
# (pattern, path glob) -> (path, line, text) of each match at the head.
Grepper = Callable[[str, str | None], list[tuple[str, int, str]]]


@dataclass
class Verdict:
	summary: str = ""
	confidence_score: int = 0
	confidence_reason: str = ""
	risk_level: str = "medium"
	risk_reason: str = ""
	diagram_type: str = "none"
	diagram: str = ""
	important_files: list[dict[str, str]] = field(default_factory=list)
	submitted: bool = False

	@classmethod
	def from_arguments(cls, args: dict[str, Any]) -> "Verdict":
		score = args.get("confidence_score")
		return cls(
			summary=str(args.get("summary") or "").strip(),
			confidence_score=min(max(int(score) if isinstance(score, int | float) else 3, 0), 5),
			confidence_reason=str(args.get("confidence_reason") or "").strip(),
			risk_level=_choice(args.get("risk_level"), ("low", "medium", "high", "critical"), "medium"),
			risk_reason=str(args.get("risk_reason") or "").strip(),
			diagram_type=_choice(args.get("diagram_type"), ("sequence", "er", "class", "flow"), "none"),
			diagram=str(args.get("diagram") or "").strip(),
			important_files=[
				f for f in args.get("important_files") or [] if isinstance(f, dict) and f.get("path")
			][:15],
			submitted=True,
		)


@dataclass
class Delta:
	"""The changes of a push since the last completed review, which reviewed the commit `since`."""

	since: str
	files: list[FileDiff]  # the diff since `since`, of the reviewed files
	summary: str = ""  # the summary of the last review
	rest: list[str] = field(default_factory=list)  # the other changed files of the pull request

	@property
	def paths(self) -> list[str]:
		return [f.path for f in self.files]

	@property
	def names(self) -> set[str]:
		"""The paths and the old paths: an entity that a rename removes has the old path."""
		return {p for f in self.files for p in (f.path, f.old_path) if p}


def _choice(value: Any, allowed: tuple[str, ...], default: str) -> str:
	return value if isinstance(value, str) and value in allowed else default


@dataclass
class ReviewRun:
	review: Any
	repository: str | None
	full_name: str
	head: Reader
	base: Reader
	list_dir: Lister
	head_sha: str
	base_sha: str
	grep: Grepper | None = None
	title: str = ""
	description: str = ""
	author: str = ""
	labels: list[str] = field(default_factory=list)
	base_branch: str = ""
	head_branch: str = ""
	pr: Any = None
	adapter: ProviderAdapter | None = None
	all_files: list[FileDiff] = field(default_factory=list)
	files: list[FileDiff] = field(default_factory=list)
	config: PullRequestConfig | None = None
	commits: list[Commit] = field(default_factory=list)
	graph: CodeGraph | None = None
	changed: list[ChangedEntity] = field(default_factory=list)
	blast: BlastRadius | None = None
	tier: str = "base"
	instructions: str = ""
	frappe_app: bool = False
	rule_files: list[tuple[str, str]] = field(default_factory=list)  # (path, text) of CLAUDE.md and similar
	# Head lines added since the last reviewed commit. None on a first review.
	fresh_lines: dict[str, set[int]] | None = None
	# On a push re-review: the agents read only this diff. `files` stays the whole pull request,
	# for the graph, the checks, the anchors and the filters. None for a full review.
	delta: Delta | None = None
	usage: Usage = field(default_factory=Usage)
	turns: int = 0  # model turns of all review agents
	rounds: int = 0  # rounds of the agents: in a round, each busy agent takes one turn
	findings: list[Finding] = field(default_factory=list)
	# Open findings of earlier reviews that still apply at this head: rows of Review Finding.
	earlier_open: list[dict[str, Any]] = field(default_factory=list)
	verdict: Verdict = field(default_factory=Verdict)
	context_used: list[dict[str, str]] = field(default_factory=list)
	notes: list[str] = field(default_factory=list)  # problems to show in the summary
	sandbox: Any = None  # guard.review.sandbox.Sandbox when runtime validation is on
	last_sandbox_output: str = ""

	def file(self, path: str) -> FileDiff | None:
		return next((f for f in self.files if f.path == path), None)

	@property
	def paths(self) -> list[str]:
		return [f.path for f in self.files]

	@property
	def cfg(self) -> PullRequestConfig:
		if self.config is None:
			raise ValueError("the review has no resolved config")
		return self.config

	@property
	def settings(self) -> dict[str, Any]:
		return self.config.settings if self.config else {}
