"""Checkpoints of the review agents, so that a review that starts again goes on where it stopped.

A deploy, a dead worker or a busy gateway ends the job of a review. The next job runs the stages
before the agents again, which takes seconds, but the agents' turns cost tokens and minutes.
After each round, the job writes the agents to a file. The next job reads it back when the key
matches: the same head, tier, system prompt, tools and diff parts.
"""

import dataclasses
import gzip
import hashlib
import json
import time
from pathlib import Path
from typing import Any

import frappe

from guard.llm.types import Message, ToolCall, ToolSpec
from guard.review.agent import Agent
from guard.review.findings import Finding
from guard.review.run import ReviewRun, Verdict

# Change this when the saved shape changes: older checkpoints then do not match.
FORMAT = 1
# A review that never ends leaves its file. The daily job removes files older than this.
MAX_AGE_SECONDS = 2 * 24 * 3600


def key(run: ReviewRun, system: str, specs: list[ToolSpec], areas: list[list[str]]) -> str:
	"""What a checkpoint must match. Another head or prompt is another review."""
	since = (run.delta.since or ",".join(str(n) for n in run.delta.originals)) if run.delta else ""
	parts = [FORMAT, run.head_sha, since, run.tier, system, [dataclasses.asdict(s) for s in specs], areas]
	text = json.dumps(parts, sort_keys=True, default=str)
	return hashlib.blake2b(text.encode(), digest_size=16).hexdigest()


def save(review: str, data: dict[str, Any]) -> None:
	path = _path(review)
	path.parent.mkdir(parents=True, exist_ok=True)
	# Write, then rename: a job killed in the middle leaves the last checkpoint whole.
	partial = path.with_name(path.name + ".partial")
	partial.write_bytes(gzip.compress(json.dumps(data, default=str).encode(), compresslevel=3))
	partial.replace(path)


def load(review: str, expected: str) -> dict[str, Any] | None:
	"""The checkpoint of the review, or None when there is none or it does not match."""
	path = _path(review)
	if not path.exists():
		return None
	try:
		data = json.loads(gzip.decompress(path.read_bytes()))
	except OSError, ValueError:
		return None
	return data if isinstance(data, dict) and data.get("key") == expected else None


def delete(review: str) -> None:
	_path(review).unlink(missing_ok=True)


def remove_old() -> None:
	"""Daily: remove the checkpoints of reviews that ended without their job."""
	folder = _folder()
	if not folder.exists():
		return
	cutoff = time.time() - MAX_AGE_SECONDS
	for path in folder.iterdir():
		if path.stat().st_mtime < cutoff:
			path.unlink(missing_ok=True)


def dump_agent(agent: Agent) -> dict[str, Any]:
	marked = next((i for i, m in enumerate(agent.messages) if m is agent.breakpoint), None)
	return {
		"messages": [dataclasses.asdict(m) for m in agent.messages],
		"turns": agent.turns,
		"done": agent.done,
		"verdict": dataclasses.asdict(agent.verdict),
		"reported": sorted(agent.reported),
		"nudged": agent.nudged,
		"tool_turns": agent.tool_turns,
		"caching": agent.caching,
		"breakpoint": marked,
	}


def restore_agent(agent: Agent, data: dict[str, Any]) -> None:
	"""Put the saved state into an agent made with the same run, system prompt and turns."""
	agent.messages = [_message(m) for m in data["messages"]]
	agent.turns = int(data["turns"])
	agent.done = bool(data["done"])
	agent.verdict = Verdict(**data["verdict"])
	agent.reported = set(data["reported"])
	agent.nudged = bool(data["nudged"])
	agent.tool_turns = [list(indexes) for indexes in data["tool_turns"]]
	agent.caching = bool(data["caching"])
	index = data.get("breakpoint")
	agent.breakpoint = agent.messages[index] if index is not None else None


def dump_findings(findings: list[Finding]) -> list[dict[str, Any]]:
	return [dataclasses.asdict(f) for f in findings]


def load_findings(rows: list[dict[str, Any]]) -> list[Finding]:
	return [Finding(**row) for row in rows]


def _message(row: dict[str, Any]) -> Message:
	calls = [ToolCall(**call) for call in row.get("tool_calls") or []]
	return Message(
		row["role"],
		row["content"],
		calls,
		row.get("tool_call_id"),
		bool(row.get("cache")),
		row.get("thinking") or [],
	)


def _folder() -> Path:
	return Path(frappe.get_site_path("private", "guard", "checkpoints"))


def _path(review: str) -> Path:
	return _folder() / f"{review}.json.gz"
