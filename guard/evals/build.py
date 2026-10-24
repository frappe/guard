"""Turn finished reviews into eval cases.

A finding that the team addressed is one Guard must find again. A finding that the team ignored
or dismissed is one Guard must not raise again. Run it on the site that did the reviews, which
has the repository mirrors:

    bench --site <site> execute guard.evals.build.from_reviews --kwargs "{'limit': 20}"

It writes `guard/evals/cases/<repo>_<number>.json`. Read each case before you commit it: the
keywords come from the finding titles, and a case must not hold secrets.
"""

import json
import os
import re
from typing import Any

import frappe
import httpx

from guard.evals.run import CASES
from guard.git.diff import parse_diff
from guard.git.repository import store_for
from guard.review.checks import mask_secrets
from guard.utils import db

KEEP = ("Addressed",)
DROP = ("Ignored", "Dismissed")
MAX_TREE = 5000
CONTEXT_FILES = re.compile(r"(^|/)(CLAUDE\.md|AGENTS\.md|guard\.toml|\.guard/[^/]+)$")
STOP_WORDS = {
	"this", "that", "with", "when", "from", "does", "will", "into", "have", "they", "then",
	"than", "only", "also", "calls", "called", "missing", "wrong", "should", "could", "still",
}  # fmt: skip


def from_reviews(limit: int = 20, write: bool = True) -> list[str]:
	"""Build cases from the newest reviews with addressed or ignored findings. Returns case names."""
	rows = db.rows(
		"""SELECT DISTINCT f.code_review FROM `tabReview Finding` f
		JOIN `tabCode Review` r ON r.name = f.code_review
		WHERE f.posted = 1 AND f.status IN %s AND r.status = 'Completed' AND r.source = 'pull_request'
		ORDER BY r.creation DESC LIMIT %s""",
		((*KEEP, *DROP), int(limit)),
	)
	names = []
	for (review,) in rows:
		case = case_from_review(str(review))
		if not case:
			continue
		if write:
			write_case(case)
		names.append(case["name"])
	return names


def write_case(case: dict[str, Any]) -> None:
	path = CASES / f"{re.sub(r'[^a-z0-9]+', '_', case['name'].lower())}.json"
	path.write_text(json.dumps(case, indent="\t", ensure_ascii=False) + "\n")


def from_github(
	full_name: str,
	number: int,
	head: str,
	base: str,
	expected: list[dict[str, Any]] | None = None,
	unexpected: list[dict[str, Any]] | None = None,
	note: str = "",
) -> dict[str, Any]:
	"""A case from one commit of a public pull request, read through the GitHub API.

	For a review that another site did, or a miss that someone found by hand. `base` is the merge
	base that the review used. Write the expectations by hand: a path, the lowest severity that
	counts, keywords that a matching finding must have, and why. Then call `write_case`.
	"""
	github = _GitHub()
	pr = github.get(f"/repos/{full_name}/pulls/{number}")
	diff = mask_secrets(github.text(f"/repos/{full_name}/compare/{base}...{head}", "diff"))
	changed = parse_diff(diff)
	tree = [
		item["path"]
		for item in github.get(f"/repos/{full_name}/git/trees/{head}", recursive=1).get("tree") or []
		if item.get("type") == "blob"
	][:MAX_TREE]
	wanted = {f.path for f in changed if f.status != "deleted"} | {p for p in tree if CONTEXT_FILES.search(p)}
	return {
		"name": f"{full_name.split('/')[-1]}-{number}",
		"source": f"https://github.com/{full_name}/pull/{number}/commits/{head[:7]}",
		"note": note,
		"payload": {
			"full_name": full_name,
			"title": pr.get("title") or "",
			"description": pr.get("body") or "",
			"branch": (pr.get("head") or {}).get("ref") or "",
			"base_branch": (pr.get("base") or {}).get("ref") or "",
			"head_sha": head,
			"base_sha": base,
			"diff": diff,
			"files": {p: mask_secrets(github.file(full_name, p, head)) for p in sorted(wanted)},
			"base_files": {
				f.path: mask_secrets(github.file(full_name, f.old_path or f.path, base))
				for f in changed
				if f.status == "modified"
			},
			"tree": tree,
		},
		"expected": expected or [],
		"unexpected": unexpected or [],
	}


class _GitHub:
	"""Read-only GitHub REST calls. GITHUB_TOKEN raises the rate limit; public repositories need none."""

	URL = "https://api.github.com"

	def __init__(self):
		token = os.environ.get("GITHUB_TOKEN")
		self.headers = {"Authorization": f"Bearer {token}"} if token else {}

	def get(self, path: str, **params: Any) -> Any:
		return self._request(path, "json", params).json()

	def text(self, path: str, kind: str) -> str:
		return self._request(path, kind, {}).text

	def file(self, full_name: str, path: str, ref: str) -> str:
		try:
			return self._request(f"/repos/{full_name}/contents/{path}", "raw", {"ref": ref}).text
		except httpx.HTTPStatusError:
			return ""

	def _request(self, path: str, kind: str, params: dict[str, Any]) -> httpx.Response:
		accept = "application/vnd.github+json" if kind == "json" else f"application/vnd.github.{kind}"
		response = httpx.get(
			f"{self.URL}{path}", params=params, headers={**self.headers, "Accept": accept}, timeout=60
		)
		response.raise_for_status()
		return response


def case_from_review(review: str) -> dict[str, Any] | None:
	row = db.values("Code Review", review, ("pull_request", "head_sha", "merge_base_sha")) or {}
	if not row.get("pull_request") or not row.get("head_sha") or not row.get("merge_base_sha"):
		return None
	pr = frappe.get_doc("Pull Request", str(row["pull_request"]))
	full_name = str(db.value("Code Repository", str(pr.get("repository")), "full_name"))
	store = store_for(str(pr.get("repository")))
	head, base = str(row["head_sha"]), str(row["merge_base_sha"])
	if not store.has_commit(head) or not store.has_commit(base):
		return None
	diff = mask_secrets(store.diff(base, head))
	changed = parse_diff(diff)
	tree = [entry.path for entry in store.ls_tree(head)][:MAX_TREE]
	wanted = {f.path for f in changed if f.status != "deleted"} | {p for p in tree if CONTEXT_FILES.search(p)}
	findings = frappe.get_all(
		"Review Finding",
		filters={"code_review": review, "posted": 1, "status": ["in", [*KEEP, *DROP]]},
		fields=["path", "severity", "title", "status", "status_reason"],
	)
	expected = [_expect(f) for f in findings if f.status in KEEP]
	unexpected = [_expect(f) for f in findings if f.status in DROP]
	if not expected and not unexpected:
		return None
	return {
		"name": f"{full_name.split('/')[-1]}-{pr.get('number')}",
		"source": f"https://github.com/{full_name}/pull/{pr.get('number')}/commits/{head[:7]}",
		"note": f"Built from review {review}.",
		"payload": {
			"full_name": full_name,
			"title": pr.get("title") or "",
			"description": pr.get("description") or "",
			"branch": pr.get("head_branch") or "",
			"base_branch": pr.get("base_branch") or "",
			"head_sha": head,
			"base_sha": base,
			"diff": diff,
			"files": {p: mask_secrets(store.show(head, p) or "") for p in sorted(wanted)},
			"base_files": {
				f.path: mask_secrets(store.show(base, f.old_path or f.path) or "")
				for f in changed
				if f.status == "modified"
			},
			"tree": tree,
		},
		"expected": expected,
		"unexpected": unexpected,
	}


def keywords(title: str, count: int = 2) -> list[str]:
	"""The longest distinct words of a title, as a loose match for a finding about the same thing."""
	words = [w for w in re.findall(r"[a-z_][a-z0-9_]{3,}", title.lower()) if w not in STOP_WORDS]
	return sorted(dict.fromkeys(words), key=len, reverse=True)[:count]


def _expect(finding: Any) -> dict[str, Any]:
	return {
		"path": finding.path,
		"severity": finding.severity,
		"keywords": keywords(finding.title or ""),
		"why": finding.title + (f" ({finding.status_reason})" if finding.status_reason else ""),
	}
