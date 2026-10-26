"""A fake GitHub adapter that records calls, and a scripted model."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from guard.llm.client import ChatRequest
from guard.llm.types import ChatResult, ToolCall, Usage
from guard.providers.base import (
	InlineComment,
	PostedComment,
	ProviderAdapter,
	ProviderComment,
	PullRequestInfo,
	Reaction,
	RepositoryInfo,
)


@dataclass
class FakeDoc:
	values: dict[str, Any] = field(default_factory=dict)
	name: str = "Fake"

	def get(self, key: str, default: Any = None) -> Any:
		return self.values.get(key, default)

	def get_password(self, *args: Any, **kwargs: Any) -> str:
		return ""


class FakeAdapter(ProviderAdapter):
	"""Keeps everything in memory. `pull_ref` points at the local head branch."""

	def __init__(self, info: PullRequestInfo, branch: str = "feature"):
		super().__init__(FakeDoc(), None)
		self.info = info
		self.branch = branch
		self.calls: list[tuple[str, Any]] = []
		self.comments: dict[str, str] = {}
		self.reactions: dict[str, list[Reaction]] = {}
		self.closed: list[tuple[str, str]] = []  # (repo, sha)
		self.checks: list[tuple[str, str, str, str]] = []  # (sha, state, description, check id)
		self._next = 100

	def _id(self) -> str:
		self._next += 1
		return str(self._next)

	def auth_headers(self) -> dict[str, str]:
		return {}

	def git_auth_header(self) -> str | None:
		return None

	def pull_ref(self, number: int) -> str:
		return f"refs/heads/{self.branch}"

	def get_pull_request(self, repo: str, number: int) -> PullRequestInfo:
		return self.info

	def get_repository(self, repo: str) -> RepositoryInfo:
		return RepositoryInfo(repo, "1", "main", "", "")

	def is_member(self, namespace: str, login: str) -> bool:
		return True

	def commit_url(self, repo: str, sha: str) -> str:
		return f"https://github.test/{repo}/commit/{sha}"

	def file_url(
		self, repo: str, sha: str, path: str, start: int | None = None, end: int | None = None
	) -> str:
		return f"https://github.test/{repo}/blob/{sha}/{path}#L{start}"

	def post_comment(self, repo: str, number: int, body: str) -> PostedComment:
		comment_id = self._id()
		self.comments[comment_id] = body
		self.calls.append(("post_comment", body))
		return PostedComment(comment_id, f"https://github.test/c/{comment_id}")

	def update_comment(self, repo: str, number: int, comment_id: str, body: str) -> None:
		self.comments[comment_id] = body
		self.calls.append(("update_comment", comment_id))

	def reply_to_comment(self, repo: str, number: int, comment: ProviderComment, body: str) -> PostedComment:
		self.calls.append(("reply", body))
		return PostedComment(self._id(), "", comment.thread_id or comment.id)

	def create_review(
		self,
		repo: str,
		number: int,
		sha: str,
		comments: list[InlineComment],
		body: str = "",
		event: str = "COMMENT",
	) -> list[PostedComment]:
		self.calls.append(("create_review", comments))
		return [PostedComment(cid := self._id(), f"https://github.test/r/{cid}", cid) for _ in comments]

	def set_status(
		self, repo: str, sha: str, state: str, description: str, url: str = "", check_id: str = ""
	) -> str:
		self.calls.append(("status", state))
		self.checks.append((sha, state, description, check_id))
		return check_id or f"check-{self._id()}"

	def close_open_checks(self, repo: str, sha: str, description: str) -> int:
		self.calls.append(("close_checks", description))
		self.closed.append((repo, sha))
		return 1

	def add_reaction(self, repo: str, number: int, target: str, target_id: str, content: str) -> str | None:
		self.calls.append(("react", content))
		reaction = self._id()
		self.calls.append(("reaction", (target, target_id, content, reaction)))
		return reaction

	def remove_reaction(self, repo: str, number: int, target: str, target_id: str, reaction_id: str) -> None:
		self.calls.append(("unreact", reaction_id))
		self.calls.append(("reaction", (target, target_id, "removed", reaction_id)))

	def list_reactions(self, repo: str, comment: ProviderComment) -> list[Reaction]:
		return self.reactions.get(comment.id, [])

	def resolve_thread(self, repo: str, number: int, thread_id: str) -> None:
		self.calls.append(("resolve", thread_id))

	def unresolve_thread(self, repo: str, number: int, thread_id: str) -> None:
		self.calls.append(("unresolve", thread_id))

	def approve(self, repo: str, number: int, sha: str, body: str = "") -> str | None:
		self.calls.append(("approve", sha))
		return "approval-1"

	def dismiss_approval(self, repo: str, number: int, approval_id: str) -> None:
		self.calls.append(("dismiss", approval_id))

	def named(self, name: str) -> list[Any]:
		return [value for call, value in self.calls if call == name]


def scripted(
	steps: list[list[tuple[str, dict[str, Any]]] | str], json_answers: dict[str, Any] | None = None
) -> Callable[[ChatRequest], ChatResult]:
	"""A responder: each turn returns the next list of tool calls, or text. Other purposes get JSON."""
	queue = list(steps)
	answers = json_answers or {}

	def respond(request: ChatRequest) -> ChatResult:
		if request.purpose in ("review", "conversation") and request.tools:
			calls = (
				queue.pop(0) if queue else [("submit_review", {"summary": "Done.", "confidence_score": 5})]
			)
			if isinstance(calls, str):
				return ChatResult(calls, [], Usage(100, 20))
			return ChatResult(
				"", [ToolCall(f"t{i}", name, args) for i, (name, args) in enumerate(calls)], Usage(100, 20)
			)
		return ChatResult(str(answers.get(request.purpose, "")), [], Usage(10, 5))

	return respond
