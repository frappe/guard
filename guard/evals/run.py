import json
from pathlib import Path
from typing import Any

import frappe

from guard.review.pipeline import ReviewPipeline

CASES = Path(__file__).parent / "cases"


def run(case: str = "", tier: str = "") -> list[dict[str, Any]]:
	"""Replay recorded pull requests through the full review and report which expected findings came back.

	`bench --site <site> execute guard.evals.run.run --kwargs "{'case': 'pilot-678'}"`. Needs the AI gateway.
	"""
	results = [review_case(item, tier) for item in load_cases(case)]
	for result in results:
		print(f"{result['case']}: {result['found']}/{result['expected']} expected, review {result['review']}")
		for why in result["missed"]:
			print(f"  missed: {why}")
	return results


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
	ReviewPipeline.run(str(review.name))
	findings = frappe.get_all(
		"Review Finding",
		filters={"code_review": review.name, "status": "Open"},
		fields=["path", "severity", "title", "body"],
	)
	missed = [e for e in case["expected"] if not any(matches(e, f) for f in findings)]
	return {
		"case": case["name"],
		"review": review.name,
		"expected": len(case["expected"]),
		"found": len(case["expected"]) - len(missed),
		"missed": [e["why"] for e in missed],
	}


def matches(expected: dict[str, Any], finding: dict[str, Any]) -> bool:
	"""Same path when the case names one, and every keyword in the title or body."""
	if expected.get("path") and expected["path"] != finding["path"]:
		return False
	text = f"{finding['title']} {finding['body']}".lower()
	return all(keyword in text for keyword in expected["keywords"])
