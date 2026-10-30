"""The Frappe review pack: checks that need no model, ported from frappe/semgrep-rules.

A config turns a rule on with `enabled_checks` and off with `disabled_checks`, by its id.
"""

from collections.abc import Callable
from typing import Any

from guard.git.diff import FileDiff
from guard.review.findings import Finding

# Importing the rule modules adds their rules to RULES.
from guard.review.frappe_pack import js_rules, py_rules
from guard.review.frappe_pack.engine import RULES, Rule, active, run


def findings(
	files: list[FileDiff],
	head: Callable[[str], str | None],
	settings: dict[str, Any],
	settings_for: Callable[[str], dict[str, Any] | None] = lambda path: None,
) -> list[Finding]:
	"""Findings of the rules that the config leaves on, on the lines that the change added.
	`settings_for(path)` gives the config of one file, when it differs from `settings`."""
	return run(files, head, settings, settings_for)


__all__ = ["RULES", "Rule", "active", "findings", "js_rules", "py_rules"]
