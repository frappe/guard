"""Tool calls that a model wrote as text.

DeepSeek writes tool calls in its DSML markup, and the server turns them into `tool_calls`. Sometimes
the model writes a tag the server does not know, for example `<|DSML| calls>` (with fullwidth bars), and
the calls come back as text. Guard then posted the markup as a reply. `recover` reads the calls from the text and
removes the markup.
"""

import json
import re
import uuid
from typing import Any

from guard.llm.types import ChatResult, ToolCall, ToolSpec

BAR = "\uff5c"  # FULLWIDTH VERTICAL LINE: DeepSeek tags are `<|DSML|name>` with this bar
OPEN = rf"<{BAR}DSML{BAR}\s*"
CLOSE = rf"</{BAR}DSML{BAR}\s*"
CALLS = r"(?:function_|tool_)?calls\s*>"
# A block that max_tokens cut off has no closing tag: it runs to the end of the text.
BLOCK = re.compile(rf"{OPEN}{CALLS}(.*?)(?:{CLOSE}{CALLS}|$)", re.DOTALL)
INVOKE = re.compile(rf'{OPEN}invoke\s+name="([^"]+)"\s*>(.*?)(?:{CLOSE}invoke\s*>|$)', re.DOTALL)
PARAMETER = re.compile(
	rf'{OPEN}parameter\s+name="([^"]+)"(?:\s+string="(true|false)")?\s*>(.*?){CLOSE}parameter\s*>', re.DOTALL
)
STRAY = re.compile(rf"</?{BAR}DSML{BAR}[^>]*>")


def recover(result: ChatResult, tools: list[ToolSpec] | None) -> ChatResult:
	"""Remove tool-call markup from the text. When the request offered tools and the answer has no
	tool calls, use the calls in the markup that name an offered tool."""
	if f"{BAR}DSML{BAR}" not in result.content:
		return result
	calls = [call for block in BLOCK.finditer(result.content) for call in _calls(block.group(1))]
	result.content = STRAY.sub("", BLOCK.sub("", result.content)).strip()
	names = {tool.name for tool in tools or []}
	if not result.tool_calls:
		result.tool_calls = [call for call in calls if call.name in names]
	return result


def _calls(block: str) -> list[ToolCall]:
	return [
		ToolCall(f"call_{uuid.uuid4().hex[:24]}", name, _arguments(body))
		for name, body in INVOKE.findall(block)
	]


def _arguments(body: str) -> dict[str, Any]:
	arguments: dict[str, Any] = {}
	for name, is_string, value in PARAMETER.findall(body):
		arguments[name] = value if is_string == "true" else _json(value)
	return arguments


def _json(value: str) -> Any:
	try:
		return json.loads(value)
	except json.JSONDecodeError:
		return value
