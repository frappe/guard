"""Config schema: normalize one TOML source into a layer and validate it."""

from dataclasses import dataclass, field
from typing import Any

from guard.config.globs import scope_patterns

EVENTS = ("open", "push", "rebase")
TIERS = ("base", "plus", "apex")
TIER_RANK = {"auto": 0, "base": 1, "plus": 2, "apex": 3}
COMMENT_TYPES = ("logic", "syntax", "style")
SEVERITIES = ("low", "medium", "high")
RISKS = ("low", "medium", "high", "critical")
SECTIONS = ("summary", "issues_table", "confidence", "impact", "diagram")
SECTION_KEYS = ("included", "collapsible", "default_open")
BOOLEANS = (
	"review_drafts",
	"status_check",
	"status_comments",
	"update_description",
	"update_summary_only",
	"fix_with_ai",
	"hide_footer",
	"comments_outside_diff",
	"include_rule_files",
)
LISTS = (
	"labels",
	"disabled_labels",
	"include_authors",
	"exclude_authors",
	"include_branches",
	"exclude_branches",
	"include_keywords",
	"ignore_keywords",
)
KNOWN_KEYS = {
	"strictness",
	"comment_types",
	"effort",
	"auto_review",
	"ignore_patterns",
	"instructions",
	"rules",
	"disabled_rules",
	"files",
	"notes",
	"context",
	"sections",
	"auto_approve",
	*BOOLEANS,
	*LISTS,
}
FILTER_KEYS = {
	"include_paths",
	"exclude_paths",
	"include_authors",
	"exclude_authors",
	"include_branches",
	"exclude_branches",
	"labels",
	"disabled_labels",
	"include_keywords",
	"ignore_keywords",
	"include_repositories",
	"exclude_repositories",
}

DEFAULTS: dict[str, Any] = {
	"strictness": 2,
	"comment_types": list(COMMENT_TYPES),
	"effort": None,
	"auto_review": ["open", "push"],
	"review_drafts": False,
	"status_check": True,
	"status_comments": False,
	"update_description": False,
	"update_summary_only": False,
	"fix_with_ai": True,
	"hide_footer": False,
	"comments_outside_diff": True,
	"include_rule_files": True,
	**{name: [] for name in LISTS},
	"sections": {name: {"included": True, "collapsible": True, "default_open": False} for name in SECTIONS},
}


@dataclass
class Rule:
	rule: str
	source: str
	id: str | None = None
	scope: list[str] = field(default_factory=list)
	severity: str | None = None
	enabled: bool = True
	context_name: str | None = None  # Custom Context record, for dashboard rules


@dataclass
class FileRef:
	path: str
	source: str
	description: str = ""
	scope: list[str] = field(default_factory=list)


@dataclass
class Note:
	content: str
	source: str
	scope: list[str] = field(default_factory=list)


@dataclass
class Layer:
	source: str
	directory: str = ""
	settings: dict[str, Any] = field(default_factory=dict)
	rules: list[Rule] = field(default_factory=list)
	files: list[FileRef] = field(default_factory=list)
	notes: list[Note] = field(default_factory=list)
	instructions: str = ""
	rules_md: str = ""
	disabled_rules: list[str] = field(default_factory=list)
	repos: list[str] = field(default_factory=list)
	ignore_patterns: list[str] = field(default_factory=list)
	auto_approve: dict[str, Any] | None = None
	errors: list[str] = field(default_factory=list)


def review_level(events: list[str]) -> int:
	"""`auto_review` as a level: 0 never, 1 open, 2 push, 3 rebase. Each implies the ones before."""
	return max((EVENTS.index(e) + 1 for e in events if e in EVENTS), default=0)


def events_for_level(level: int) -> list[str]:
	return list(EVENTS[:level])


def normalize(raw: Any, source: str, directory: str = "") -> Layer:
	"""Turn one parsed config into a layer. An invalid config gives a layer with errors only."""
	layer = Layer(source=source, directory=directory)
	if not isinstance(raw, dict):
		layer.errors.append(f"{source}: the config must be a table")
		return layer
	errors = validate(raw)
	if errors:
		layer.errors = [f"{source}: {e}" for e in errors]
		return layer
	settings = layer.settings
	for key in ("strictness", "effort"):
		if key in raw:
			settings[key] = raw[key]
	if "comment_types" in raw:
		settings["comment_types"] = list(raw["comment_types"])
	if "auto_review" in raw:
		settings["auto_review"] = events_for_level(review_level(raw["auto_review"]))
	for key in BOOLEANS:
		if key in raw:
			settings[key] = raw[key]
	for key in LISTS:
		if key in raw:
			settings[key] = [str(v) for v in raw[key]]
	sections = raw.get("sections") or {}
	if sections:
		settings["sections"] = {
			name: {k: v for k, v in values.items() if k in SECTION_KEYS} for name, values in sections.items()
		}
	_context(raw, layer, directory)
	if isinstance(raw.get("auto_approve"), dict):
		layer.auto_approve = {k: v for k, v in raw["auto_approve"].items() if k != "risk_definitions"}
	return layer


def _context(raw: dict, layer: Layer, directory: str) -> None:
	source = layer.source
	layer.instructions = str(raw.get("instructions") or "").strip()
	layer.ignore_patterns = (
		scope_patterns(directory, raw["ignore_patterns"]) if raw.get("ignore_patterns") else []
	)
	for item in raw.get("rules") or []:
		layer.rules.append(
			Rule(
				rule=item["rule"],
				source=source,
				id=item.get("id"),
				scope=scope_patterns(directory, item.get("scope")),
				severity=item.get("severity"),
				enabled=item.get("enabled", True),
			)
		)
	layer.files += files_from(raw.get("files") or [], source, directory)
	for item in raw.get("notes") or []:
		layer.notes.append(
			Note(content=item["content"], source=source, scope=scope_patterns(directory, item.get("scope")))
		)
	layer.disabled_rules = [str(r) for r in raw.get("disabled_rules") or []]
	layer.repos = [str(r) for r in (raw.get("context") or {}).get("repos") or []]


def files_from(items: list[dict], source: str, directory: str) -> list[FileRef]:
	return [
		FileRef(
			path=f"{directory}/{item['path']}".strip("/") if directory else item["path"],
			source=source,
			description=item.get("description") or "",
			scope=scope_patterns(directory, item.get("scope")),
		)
		for item in items
	]


def validate(raw: dict) -> list[str]:
	errors = [f"unknown key `{key}`" for key in raw if key not in KNOWN_KEYS]
	if "strictness" in raw and raw["strictness"] not in (1, 2, 3):
		errors.append("strictness must be 1, 2 or 3")
	if "comment_types" in raw and not _list_of(raw["comment_types"], COMMENT_TYPES):
		errors.append("comment_types must be an array of logic, syntax, style")
	if "effort" in raw and raw["effort"] not in (*TIERS, "auto"):
		errors.append("effort must be base, plus, apex or auto")
	if "auto_review" in raw and not _list_of(raw["auto_review"], EVENTS):
		errors.append("auto_review must be an array of open, push, rebase")
	for key in (*LISTS, "ignore_patterns", "disabled_rules"):
		if key in raw and not _list_of(raw[key]):
			errors.append(f"{key} must be an array of strings")
	for key in BOOLEANS:
		if key in raw and not isinstance(raw[key], bool):
			errors.append(f"{key} must be true or false")
	if "instructions" in raw and not isinstance(raw["instructions"], str):
		errors.append("instructions must be a string")
	errors += _validate_items(raw, "rules", "rule")
	errors += _validate_items(raw, "files", "path")
	errors += _validate_items(raw, "notes", "content")
	for index, item in enumerate(raw.get("rules") or []):
		if isinstance(item, dict) and item.get("severity") and item["severity"] not in SEVERITIES:
			errors.append(f"rules[{index}].severity must be low, medium or high")
	for name, values in (raw.get("sections") or {}).items():
		if name not in SECTIONS or not isinstance(values, dict):
			errors.append(f"unknown section `{name}`")
	errors += _validate_auto_approve(raw.get("auto_approve"))
	return errors


def _validate_items(raw: dict, key: str, required: str) -> list[str]:
	items = raw.get(key)
	if items is None:
		return []
	if not isinstance(items, list):
		return [f"{key} must be an array of tables"]
	return [
		f"{key}[{index}] needs a `{required}` string"
		for index, item in enumerate(items)
		if not isinstance(item, dict) or not isinstance(item.get(required), str)
	]


def _validate_auto_approve(value: Any) -> list[str]:
	if value is None:
		return []
	if not isinstance(value, dict):
		return ["auto_approve must be a table"]
	errors = []
	if value.get("risk_ceiling", "low") not in RISKS:
		errors.append("auto_approve.risk_ceiling must be low, medium, high or critical")
	for key in value.get("filters") or {}:
		if key not in FILTER_KEYS:
			errors.append(f"unknown auto_approve filter `{key}`")
	return errors


def _list_of(value: Any, allowed: tuple[str, ...] | None = None) -> bool:
	if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
		return False
	return allowed is None or set(value) <= set(allowed)
