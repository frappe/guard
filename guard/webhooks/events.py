"""The provider-neutral webhook event."""

from dataclasses import dataclass, field

from guard.providers.base import ProviderComment, PullRequestInfo, RepositoryInfo


@dataclass
class ReactionEvent:
	content: str
	user: str
	comment_id: str
	removed: bool = False


@dataclass
class WebhookEvent:
	kind: str  # pull_request, comment, reaction, push, installation, rerun, ignored
	action: str = ""
	repository: RepositoryInfo | None = None
	namespace: str = ""
	number: int | None = None
	pull_request: PullRequestInfo | None = None
	before: str | None = None
	after: str | None = None
	comment: ProviderComment | None = None
	reaction: ReactionEvent | None = None
	sender: str = ""
	label: str | None = None
	ref: str | None = None
	installation_id: str | None = None
	repositories: list[RepositoryInfo] = field(default_factory=list)


IGNORED = WebhookEvent(kind="ignored")
