import json
import os
import time
from pathlib import Path
from typing import Any

import frappe

from guard.llm.types import GatewayBilling
from guard.review.findings import SEVERITIES
from guard.review.pipeline import ReviewPipeline
from guard.utils import db
from guard.utils.redact import redact

CASES = Path(__file__).parent / "cases"
SUMMARY_START = "<!-- guard-evals -->"
SUMMARY_END = "<!-- /guard-evals -->"
MAX_PARKS = 8  # a review that parks more often than this does not count
PARK_WAIT = 90  # seconds: a rate limit on the eval key passes in a minute or two


def run(case: str = "", tier: str = "") -> list[dict[str, Any]]:
	"""Replay recorded pull requests through the full review and report which expected findings came back.

	`bench --site <site> execute guard.evals.run.run --kwargs "{'case': 'pilot-678'}"`. Needs the AI gateway.
	"""
	results = []
	for item in load_cases(case):
		results.append(review_case(item, tier))
		if results[-1].get("billing"):
			# Every later review would stop at its first model call: do not index and prepare them.
			break
	for result in results:
		if result["status"] != "Completed":
			print(f"{result['case']}: review {result['review']} is {result['status']}: {result['error']}")
			continue
		print(
			f"{result['case']}: {result['found']}/{result['expected']} expected, "
			f"{len(result['raised'])}/{result['unexpected']} rejected raised again, review {result['review']}"
		)
		for why in result["missed"]:
			print(f"  missed: {why}")
		for why in result["raised"]:
			print(f"  raised again: {why}")
	return results


def ci() -> None:
	"""Run every case in CI and fail on a review that does not finish, or on low recall.

	Reads the gateway from GUARD_EVAL_GATEWAY_URL, GUARD_EVAL_GATEWAY_KEY, GUARD_EVAL_MODEL and
	GUARD_EVAL_DIALECT. Without them it skips. EVAL_MIN_RECALL (0 to 1, default 0) is the share of
	expected findings that must come back. Prints a Markdown table between SUMMARY_START and
	SUMMARY_END, which the workflow adds to the job summary.
	"""
	env = os.environ
	if not env.get("GUARD_EVAL_GATEWAY_URL") or not env.get("GUARD_EVAL_MODEL"):
		print("Evals skipped: set GUARD_EVAL_GATEWAY_URL and GUARD_EVAL_MODEL.")
		return
	settings = frappe.get_single("Guard Settings")
	model = env["GUARD_EVAL_MODEL"]
	settings.update(
		{
			"gateway_url": env["GUARD_EVAL_GATEWAY_URL"],
			"gateway_api_key": env.get("GUARD_EVAL_GATEWAY_KEY", ""),
			"gateway_dialect": env.get("GUARD_EVAL_DIALECT") or "openai",
			"base_model": model,
			"plus_model": model,
			"apex_model": model,
			"max_concurrent_reviews": 0,
		}
	)
	settings.save(ignore_permissions=True)
	db.commit()
	results = run()
	summary = score(results)
	print(f"{SUMMARY_START}\n{summary['markdown']}{SUMMARY_END}")
	print(summary["line"])
	if billing := [r["error"] for r in results if r.get("billing")]:
		raise EvalFailed(f"The AI gateway refused the eval key, so the evals stopped: {billing[0]}")
	if summary["unfinished"]:
		raise EvalFailed(f"Reviews did not finish: {', '.join(summary['unfinished'])}")
	if summary["recall"] < float(env.get("EVAL_MIN_RECALL") or 0):
		raise EvalFailed(f"Recall {summary['recall']:.0%} is below EVAL_MIN_RECALL")


class EvalFailed(Exception):
	pass


def score(results: list[dict[str, Any]]) -> dict[str, Any]:
	"""Totals over all cases: recall of expected findings, and rejected findings raised again."""
	done = [r for r in results if r["status"] == "Completed"]
	expected = sum(r["expected"] for r in done)
	found = sum(r["found"] for r in done)
	unexpected = sum(r["unexpected"] for r in done)
	raised = sum(len(r["raised"]) for r in done)
	recall = found / expected if expected else 1.0
	rows = "\n".join(
		f"| {r['case']} | {r['found']}/{r['expected']} | {len(r['raised'])}/{r['unexpected']} |"
		if r["status"] == "Completed"
		else f"| {r['case']} | {r['status']}: {r.get('error') or ''} | |"
		for r in results
	)
	line = f"Found {found}/{expected} expected ({recall:.0%}). Raised {raised}/{unexpected} rejected findings again."
	return {
		"recall": recall,
		"raised": raised,
		"unfinished": [r["case"] for r in results if r["status"] != "Completed"],
		"line": line,
		"markdown": f"## Guard evals\n\n{line}\n\n| Case | Expected found | Rejected raised again |\n| - | - | - |\n{rows}\n",
	}


def load_cases(name: str = "") -> list[dict[str, Any]]:
	cases = [json.loads(path.read_text()) for path in sorted(CASES.glob("*.json"))]
	return [case for case in cases if not name or case["name"] == name]


def review_case(case: dict[str, Any], tier: str = "") -> dict[str, Any]:
	payload = case["payload"]
	review = frappe.get_doc(
		{
			"doctype": "Code Review",
			"source": "cli",
			"status": "Queued",
			"trigger": "cli",
			"requested_tier": tier,
			"head_sha": payload.get("head_sha"),
			"branch": payload.get("branch"),
			"cli_payload": frappe.as_json(payload),
			"triggered_by_user": frappe.session.user,
		}
	).insert(ignore_permissions=True)
	state = run_until_done(str(review.name))
	status = state.get("status")
	if status != "Completed":
		# Failed, or still parked after the last wait: there is nothing to score.
		error = state.get("error") or state.get("stage")
		return {
			"case": case["name"],
			"review": review.name,
			"status": status,
			"error": error,
			"billing": bool(state.get("billing")),
		}
	findings = frappe.get_all(
		"Review Finding",
		filters={"code_review": review.name, "status": "Open"},
		fields=["path", "severity", "title", "body"],
	)
	missed = [e for e in case["expected"] if not any(matches(e, f) for f in findings)]
	# A finding the team rejected, raised again at any severity.
	unexpected = case.get("unexpected") or []
	raised = [u for u in unexpected if any(matches({**u, "severity": "P2"}, f) for f in findings)]
	return {
		"case": case["name"],
		"review": review.name,
		"status": status,
		"expected": len(case["expected"]),
		"found": len(case["expected"]) - len(missed),
		"missed": [e["why"] for e in missed],
		"unexpected": len(unexpected),
		"raised": [u["why"] for u in raised],
	}


def run_until_done(review: str) -> dict[str, Any]:
	"""Run the review, and run it again each time it parks on a busy gateway.

	No scheduler runs here to pick up a parked review, so the eval waits for it. The wait is short:
	an eval run has a time limit, and a parked review keeps the turns it already used. A billing
	error (402) does not pass while the eval waits, so the review fails at once.
	"""
	fields = ("status", "error", "stage", "attempt", "retry_at")
	pipeline = ReviewPipeline.run(review)
	state = db.values("Code Review", review, fields) or {}
	for _ in range(MAX_PARKS):
		if state.get("status") != "Queued":
			break
		if isinstance(pipeline.busy, GatewayBilling):
			pipeline.fail(redact(f"AI gateway billing: {pipeline.busy}"))
			return {**(db.values("Code Review", review, fields) or {}), "billing": True}
		print(f"{review} waits: {state.get('stage')}")
		time.sleep(PARK_WAIT)
		db.set_values("Code Review", review, {"retry_at": None})
		db.commit()
		pipeline = ReviewPipeline.run(review, int(state.get("attempt") or 0))
		state = db.values("Code Review", review, fields) or {}
	return state


def matches(expected: dict[str, Any], finding: dict[str, Any]) -> bool:
	"""Same path when the case names one, at least the expected severity, and every keyword in the title or body."""
	if expected.get("path") and expected["path"] != finding["path"]:
		return False
	if SEVERITIES.index(finding["severity"]) > SEVERITIES.index(expected.get("severity", "P2")):
		return False
	text = f"{finding['title']} {finding['body']}".lower()
	return all(keyword in text for keyword in expected["keywords"])
