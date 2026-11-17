"""Backports: find the pull requests that a backport carries, and the files that Guard reviewed there.

A backport to a release branch carries changes that were reviewed on the default branch. When a file
makes only changes that a merged original makes, and Guard reviewed that original, the agent does not
review that change again. It checks only that the change fits the target branch: the same diff can be
right on the default branch and wrong on a release branch. The verdict always comes from the agent of
the backport, never from the originals. See docs/spec/05-review-engine.md.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import frappe

from guard.git.diff import FileDiff, Hunk, parse_diff
from guard.git.store import GitError, GitStore
from guard.providers.base import ProviderAdapter
from guard.review.run import Delta, ReviewRun
from guard.utils import db

# "backport #12", "(backport #12)", "Backport of #12": in the title, the description and the commits.
NAMED = re.compile(r"\bbackport(?:ed)?(?:\s+(?:of|from))?\s+#(\d+)", re.I)
# The subject of a squash merge or a cherry-pick of one ends with the number of its pull request.
SQUASHED = re.compile(r"\(#(\d+)\)\s*$")
MERGED = re.compile(r"^Merge pull request #(\d+)")
# Branches of backport bots, for example `mergify/bp/version-16-hotfix/pr-534`, `backport/master/pr-530`.
BRANCH = re.compile(r"(?:^|/)(?:bp|backport)/.+/pr-(\d+)$")
MAX_ORIGINALS = 30
SUMMARY_LIMIT = 1500  # characters of the summary of each original in the task


@dataclass
class Original:
	"""An original pull request, with its last review in which a model read the code."""

	number: int
	summary: str
	files: list[FileDiff] = field(default_factory=list)


def numbers(run: ReviewRun, number: int) -> list[int]:
	"""The numbers of the pull requests that this one backports, in the order found. Only a pull
	request to another branch than the default one is a backport: on the default branch, the subjects
	of merged pull requests come from merges of other branches."""
	found: list[int] = []
	texts = [run.title, run.description, *(c.subject for c in run.commits), *(c.body for c in run.commits)]
	for text in texts:
		found += [int(n) for n in NAMED.findall(text or "")]
	for commit in run.commits:
		for pattern in (SQUASHED, MERGED):
			if match := pattern.search(commit.subject or ""):
				found.append(int(match.group(1)))
	if match := BRANCH.search(run.head_branch or ""):
		found.append(int(match.group(1)))
	return [n for n in dict.fromkeys(found) if n != number][:MAX_ORIGINALS]


def is_backport(run: ReviewRun) -> bool:
	"""The pull request goes to another branch than the default branch of the repository."""
	default = db.value("Code Repository", run.repository, "default_branch") if run.repository else None
	return bool(default) and run.base_branch != default


def link(pull_request: str, found: list[int]) -> None:
	"""Show the originals on the Pull Request."""
	text = ", ".join(f"#{n}" for n in found)
	if (db.value("Pull Request", pull_request, "backport_of") or "") != text:
		db.set_values("Pull Request", pull_request, {"backport_of": text})


def originals(
	repository: str, found: list[int], store: GitStore, adapter: ProviderAdapter | None
) -> list[Original]:
	"""The merged originals that Guard reviewed with a model, each with the diff of that review."""
	result = []
	for number in found:
		pr = db.values("Pull Request", {"repository": repository, "number": number}, ("name", "state"))
		# Only a merged original: the title and the commits of a backport can name any pull request.
		row = _last_read(str(pr["name"])) if pr and pr["state"] == "merged" else None
		if not row:
			continue
		files = _diff(store, adapter, number, str(row["merge_base_sha"] or ""), str(row["head_sha"] or ""))
		if not files:
			continue
		result.append(
			Original(
				number,
				str(row.get("summary") or ""),
				files,
			)
		)
	return result


def _last_read(pull_request: str) -> dict[str, Any] | None:
	rows = db.dict_rows(
		"""SELECT head_sha, merge_base_sha, summary FROM `tabCode Review`
		WHERE pull_request = %s AND status = 'Completed' AND agent_turns > 0 AND source != 'local'
		ORDER BY creation DESC LIMIT 1""",
		(pull_request,),
	)
	return rows[0] if rows else None


def _diff(
	store: GitStore, adapter: ProviderAdapter | None, number: int, merge_base: str, head: str
) -> list[FileDiff]:
	"""The diff that the review of an original read. Empty when its commits are gone."""
	if not merge_base or not head:
		return []
	try:
		if not store.has_commit(head) and adapter and adapter.pull_ref(number):
			store.fetch_pull(adapter.pull_ref(number), number)
		if not (store.has_commit(head) and store.has_commit(merge_base)):
			return []
		return parse_diff(store.diff(merge_base, head))
	except GitError:
		return []


def changed_lines(hunks: Iterable[Hunk]) -> list[tuple[str, str]]:
	"""The added and removed lines in order, with their indent, which can change what code does.
	Blank lines do not count."""
	return [
		(line.kind, line.text.rstrip())
		for hunk in hunks
		for line in hunk.lines
		if line.kind in "+-" and line.text.strip()
	]


def covered(file: FileDiff, found: list[Original]) -> bool:
	"""The file makes only changes that an original makes to the same file.

	Each hunk makes its changes in the order of one original, and no line changes more often than in
	the originals. A backport can leave part of an original out. It cannot add, move or repeat lines.
	"""
	names = {file.path, file.old_path} - {""}
	sources = [
		changed_lines(other.hunks)
		for original in found
		for other in original.files
		if {other.path, other.old_path} & names
	]
	if not sources:
		return False
	if Counter(changed_lines(file.hunks)) - Counter(line for source in sources for line in source):
		return False
	return all(any(_in_order(changed_lines([hunk]), source) for source in sources) for hunk in file.hunks)


def _in_order(lines: list[tuple[str, str]], source: list[tuple[str, str]]) -> bool:
	"""`lines` is a subsequence of `source`."""
	rest = iter(source)
	return all(line in rest for line in lines)


def delta(run: ReviewRun, found: list[Original]) -> Delta | None:
	"""What the agent reads of a backport: the files that are not covered, to review, and the covered
	files, to check that they fit the target branch. None when no file is covered."""
	if not found:
		return None
	same = [f for f in run.files if covered(f, found)]
	if not same:
		return None
	paths = {f.path for f in same}
	# Only the summaries go into the task, as context. The diagram and the important files of the
	# originals show the default branch, so the agent of the backport sends its own.
	return Delta(
		since="",
		files=[f for f in run.files if f.path not in paths],
		summary="\n\n".join(f"#{o.number}: {o.summary[:SUMMARY_LIMIT]}" for o in found if o.summary),
		rest=[f"{f.path} (+{f.additions} -{f.deletions})" for f in same],
		originals=[o.number for o in found],
		covered=same,
	)
