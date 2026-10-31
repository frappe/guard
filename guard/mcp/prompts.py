"""The prompts of the MCP server. Claude Code shows them as slash commands, such as /mcp__guard__review.

A prompt only starts a task. The server instructions say how to do it.
"""

from dataclasses import dataclass, field
from typing import Any

from frappe.mcp.protocol import INVALID_PARAMS, McpError

MAX_ARGUMENT = 2000  # characters


@dataclass(frozen=True)
class Prompt:
	name: str
	title: str
	description: str
	text: str
	arguments: tuple[dict[str, Any], ...] = field(default_factory=tuple)

	def definition(self) -> dict[str, Any]:
		return {
			"name": self.name,
			"title": self.title,
			"description": self.description,
			"arguments": list(self.arguments),
		}


_PROMPTS = (
	Prompt(
		"review",
		"Review my change",
		"Review the local change with Guard before a push.",
		"Review my local change with Guard before I push. Call the Guard tool start_review, run the "
		"command of its answer, call review_context, review the change as the guide says, show me the "
		"findings and call submit_review.{focus}",
		({"name": "focus", "description": "What the review must look at.", "required": False},),
	),
	Prompt(
		"fix",
		"Fix the findings of my pull request",
		"Fix what Guard found on the pull request of the current branch.",
		"Call the Guard tool pr_findings for the current branch. Fix each finding that is correct, run "
		"the tests, and tell me which findings you think are wrong, with the reason. Do not push.",
	),
	Prompt(
		"ask",
		"Ask about the code of the team",
		"Ask Guard about code in all repositories and the team rules.",
		"Answer with the Guard tools find_symbol, search_code, team_rules and explain_config: {question}",
		({"name": "question", "description": "The question.", "required": True},),
	),
)
PROMPTS = {prompt.name: prompt for prompt in _PROMPTS}


def definitions() -> list[dict[str, Any]]:
	return [prompt.definition() for prompt in PROMPTS.values()]


def get(params: dict[str, Any]) -> dict[str, Any]:
	prompt = PROMPTS.get(str(params.get("name")))
	if prompt is None:
		raise McpError(INVALID_PARAMS, f"Unknown prompt: {params.get('name')}. Call prompts/list.")
	arguments = params.get("arguments") or {}
	if not isinstance(arguments, dict):
		raise McpError(INVALID_PARAMS, "arguments must be an object")
	values = {key: str(arguments.get(key) or "").strip()[:MAX_ARGUMENT] for key in ("focus", "question")}
	if any(a["required"] and not values[a["name"]] for a in prompt.arguments):
		raise McpError(INVALID_PARAMS, f"The prompt {prompt.name} needs an argument.")
	focus = f" Focus on: {values['focus']}" if values["focus"] else ""
	text = prompt.text.format(focus=focus, question=values["question"])
	return {
		"description": prompt.description,
		"messages": [{"role": "user", "content": {"type": "text", "text": text}}],
	}
