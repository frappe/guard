"""Decide if a pull request gets a review, and at which tier."""

import json
import re
from dataclasses import dataclass, field
from typing import Any

from guard.config.globs import match_any_filter, match_filter, match_path
from guard.config.resolver import PullRequestConfig, event_allowed, tier_for

RISKY_PATH = re.compile(
	r"auth|security|secret|payment|billing|invoice|migrat|permission|crypt|infra|deploy|\.github/workflows",
	re.I,
)
RISKY_LABELS = {"security", "breaking"}


@dataclass
class PullRequestFacts:
	repository: str  # owner/repo
	title: str
	description: str
	author: str
	base_branch: str
	head_branch: str
	is_draft: bool = False
	conflicts: bool = False  # a merge into the target branch has conflicts
	labels: list[str] = field(default_factory=list)
	paths: list[str] = field(default_factory=list)  # every changed path, both sides of renames
	additions: int = 0
	deletions: int = 0

	@property
	def files_changed(self) -> int:
		return len(set(self.paths))


@dataclass
class GateResult:
	allowed: bool
	reason: str = ""
	tier_rules: list[str] = field(default_factory=list)


@dataclass
class FilterRule:
	label: str
	conditions: list[dict[str, Any]]
	review_tier: str = ""


def check(
	facts: PullRequestFacts, config: PullRequestConfig, event: str, manual: bool, rules: list[FilterRule]
) -> GateResult:
	settings = config.settings
	tier_rules = [rule.review_tier for rule in rules if rule.review_tier and rule_matches(rule, facts)]
	if manual:
		if match_filter(facts.base_branch, settings["exclude_branches"]):
			return GateResult(False, "target branch is excluded")
		if match_filter(facts.author, settings["exclude_authors"]):
			return GateResult(False, "author is excluded")
		return GateResult(True, tier_rules=tier_rules)
	if facts.is_draft and not settings["review_drafts"]:
		return GateResult(False, "draft pull request")
	if facts.conflicts:
		return GateResult(False, "merge conflicts with the target branch")
	if not event_allowed(settings, event):
		return GateResult(False, f"automatic review is off for `{event}`")
	ok, reason = filters_pass(settings, facts)
	if not ok:
		return GateResult(False, reason)
	gates = [rule for rule in rules if not rule.review_tier]
	if gates and not any(rule_matches(rule, facts) for rule in gates):
		return GateResult(False, "no dashboard filter rule matches")
	return GateResult(True, tier_rules=tier_rules)


def filters_pass(settings: dict[str, Any], facts: PullRequestFacts) -> tuple[bool, str]:
	text = f"{facts.title}\n{facts.description}".lower()
	checks = (
		(settings["labels"] and not match_any_filter(facts.labels, settings["labels"]), "no required label"),
		(match_any_filter(facts.labels, settings["disabled_labels"]), "a label skips review"),
		(
			settings["include_authors"] and not match_filter(facts.author, settings["include_authors"]),
			"author is not included",
		),
		(match_filter(facts.author, settings["exclude_authors"]), "author is excluded"),
		(
			settings["include_branches"]
			and not match_filter(facts.base_branch, settings["include_branches"]),
			"target branch is not included",
		),
		(match_filter(facts.base_branch, settings["exclude_branches"]), "target branch is excluded"),
		(
			settings["include_keywords"] and not any(k.lower() in text for k in settings["include_keywords"]),
			"no required keyword",
		),
		(any(k.lower() in text for k in settings["ignore_keywords"]), "a keyword skips review"),
	)
	for failed, reason in checks:
		if failed:
			return False, reason
	return True, ""


def rule_matches(rule: FilterRule, facts: PullRequestFacts) -> bool:
	return all(condition_matches(c, facts) for c in rule.conditions)


def condition_matches(condition: dict[str, Any], facts: PullRequestFacts) -> bool:
	field_name = condition.get("field")
	operator = condition.get("operator")
	values = [str(v) for v in condition.get("values") or []]
	if field_name == "files_changed":
		limit = int(values[0]) if values and values[0].isdigit() else 0
		return facts.files_changed > limit if operator == "more_than" else facts.files_changed < limit
	hit = _field_hit(field_name or "", values, facts)
	return not hit if operator == "exclude" else hit


def _field_hit(field_name: str, values: list[str], facts: PullRequestFacts) -> bool:
	if field_name == "author":
		return match_filter(facts.author, values)
	if field_name == "target_branch":
		return match_filter(facts.base_branch, values)
	if field_name == "source_branch":
		return match_filter(facts.head_branch, values)
	if field_name == "label":
		return match_any_filter(facts.labels, values)
	if field_name == "repository":
		return match_filter(facts.repository, values)
	if field_name == "title":
		return any(v.lower() in facts.title.lower() for v in values)
	if field_name == "keyword":
		text = f"{facts.title}\n{facts.description}".lower()
		return any(v.lower() in text for v in values)
	if field_name == "file_path":
		return any(match_path(path, values) for path in facts.paths)
	return False


def dashboard_rules(settings_doc: Any) -> list[FilterRule]:
	rules = []
	for row in settings_doc.get("filter_rules") or []:
		if not row.get("enabled"):
			continue
		conditions = row.get("conditions")
		try:
			parsed = json.loads(conditions) if isinstance(conditions, str) else conditions or []
		except json.JSONDecodeError:
			continue
		rules.append(FilterRule(row.get("label") or "", parsed, row.get("review_tier") or ""))
	return rules


def select_tier(
	config: PullRequestConfig,
	tier_rules: list[str],
	requested: str | None,
	facts: PullRequestFacts,
	contract_changes: int = 0,
	first_review: bool = False,
) -> str:
	tier = tier_for(config.efforts, tier_rules, requested)
	return tier if tier != "auto" else auto_tier(facts, contract_changes, first_review)


def auto_tier(facts: PullRequestFacts, contract_changes: int = 0, first_review: bool = False) -> str:
	"""Pick a tier from size, risk and complexity. A first review is at least plus: size is not risk."""
	points = (facts.additions + facts.deletions) / 200 + facts.files_changed / 10 + contract_changes * 0.5
	if any(RISKY_PATH.search(path) for path in facts.paths):
		points += 2
	if RISKY_LABELS & {label.lower() for label in facts.labels}:
		points += 2
	if points < 2:
		return "plus" if first_review else "base"
	return "plus" if points < 5 else "apex"


def auto_approve_filters_pass(filters: dict[str, Any], facts: PullRequestFacts) -> tuple[bool, str]:
	"""Auto-approve filters. Paths cover both sides of renames through `facts.paths`."""
	if filters.get("exclude_paths") and any(match_path(p, filters["exclude_paths"]) for p in facts.paths):
		return False, "touches an excluded path"
	if filters.get("include_paths") and not all(match_path(p, filters["include_paths"]) for p in facts.paths):
		return False, "a path is outside the included paths"
	if filters.get("include_repositories") and not match_filter(
		facts.repository, filters["include_repositories"]
	):
		return False, "repository is not included"
	if match_filter(facts.repository, filters.get("exclude_repositories")):
		return False, "repository is excluded"
	return filters_pass(
		{**_EMPTY_FILTERS, **{k: v for k, v in filters.items() if k in _EMPTY_FILTERS}}, facts
	)


_EMPTY_FILTERS: dict[str, Any] = {
	"labels": [],
	"disabled_labels": [],
	"include_authors": [],
	"exclude_authors": [],
	"include_branches": [],
	"exclude_branches": [],
	"include_keywords": [],
	"ignore_keywords": [],
}
