"""Whitelisted methods of local reviews, for coding agents. Agents call them with frappectl, or with
the tool `call_method` of the MCP server of Frappe (`/api/v2/mcp`). The agent reviews the change
with its own model, so Guard spends no tokens. See docs/spec/11-surfaces.md (Local reviews)."""

from typing import Any

import frappe
from frappe.rate_limiter import rate_limit

from guard.review import local

STARTS_PER_HOUR = 30  # a start applies a diff and reads the graph: cheap, but not free


@frappe.whitelist(methods=["GET"])
def guide() -> str:
	"""How a coding agent runs a Guard review of a local change before it is pushed. Read this first."""
	return local.guide()


@frappe.whitelist(methods=["POST"])
@rate_limit(limit=STARTS_PER_HOUR, seconds=60 * 60, user_based=True, ip_based=False)
def start(
	full_name: str, base: str, diff: str, branch: str = "", head: str = "", instructions: str = ""
) -> dict[str, Any]:
	"""Start a local review. `full_name` is `owner/name`, `base` the merge base with the default
	branch, `diff` the output of `git diff --binary <base>`. Returns `review_id`, the findings of
	Guard's checks, and `guide`: the rubric, the rules and the graph facts. See `guide`."""
	return local.start(full_name, base, diff, branch, head, instructions)


@frappe.whitelist(methods=["GET"])
def symbol(full_name: str, name: str) -> dict[str, Any]:
	"""Definitions of a function, class or method on the default branch, with callers and callees."""
	return local.symbol(full_name, name)


@frappe.whitelist(methods=["POST"])
def submit(
	review_id: str,
	findings: list[dict[str, Any]],
	summary: str = "",
	confidence_score: int | None = None,
	confidence_reason: str = "",
	risk_level: str = "",
	risk_reason: str = "",
) -> dict[str, Any]:
	"""Finish a local review: save the findings of the agent with those of Guard's checks. Guard posts
	nothing to the code provider. Returns the link to the Code Review, and the kept and dropped
	findings."""
	return local.submit(
		review_id, findings, summary, confidence_score, confidence_reason, risk_level, risk_reason
	)
