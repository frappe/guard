"""Check if a call still fits a changed signature. Python only.

The answer is True (fits), False (breaks) or None (cannot tell). Callers treat None
as a possible break.
"""

import ast
import re
from dataclasses import dataclass

MAX_CALL_LINES = 30
BOUND = ("self", "cls")


@dataclass
class Params:
	positional: list[str]  # positional-or-keyword, in order
	required: int  # how many of `positional` have no default
	keyword_only: set[str]
	required_keyword: set[str]
	var_positional: bool
	var_keyword: bool


@dataclass
class Call:
	positional: int
	keywords: set[str]
	star_args: bool
	star_kwargs: bool


def parse_params(signature: str) -> Params | None:
	"""Parameters from a Python `def` line. None for other languages."""
	text = " ".join((signature or "").split()).rstrip(":").strip()
	if not text.startswith(("def ", "async def ")):
		return None
	try:
		node = ast.parse(f"{text}: pass").body[0]
	except SyntaxError:
		return None
	if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
		return None
	args = node.args
	positional = [a.arg for a in [*args.posonlyargs, *args.args]]
	defaults = len(args.defaults)
	required = len(positional) - defaults
	if positional and positional[0] in BOUND:
		positional, required = positional[1:], max(required - 1, 0)
	keyword_only = {a.arg for a in args.kwonlyargs}
	required_keyword = {a.arg for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True) if d is None}
	return Params(
		positional, required, keyword_only, required_keyword, args.vararg is not None, args.kwarg is not None
	)


def parse_call(lines: list[str], line: int, name: str) -> Call | None:
	"""The call of `name` that starts on `line` (1-based) of `lines`."""
	if not 1 <= line <= len(lines):
		return None
	text = "\n".join(lines[line - 1 : line - 1 + MAX_CALL_LINES])
	match = re.search(rf"\b{re.escape(name)}\s*\(", text)
	if not match:
		return None
	inner = _balanced(text, match.end())
	if inner is None:
		return None
	try:
		node = ast.parse(f"f({inner})", mode="eval").body
	except SyntaxError:
		return None
	if not isinstance(node, ast.Call):
		return None
	return Call(
		positional=sum(1 for a in node.args if not isinstance(a, ast.Starred)),
		keywords={k.arg for k in node.keywords if k.arg},
		star_args=any(isinstance(a, ast.Starred) for a in node.args),
		star_kwargs=any(k.arg is None for k in node.keywords),
	)


def fits(params: Params, call: Call) -> bool | None:
	if call.star_args or call.star_kwargs:
		return None
	if call.positional > len(params.positional) and not params.var_positional:
		return False
	given = set(params.positional[: call.positional]) | call.keywords
	if any(name not in given for name in params.positional[: params.required]):
		return False
	if params.required_keyword - call.keywords:
		return False
	known = set(params.positional) | params.keyword_only
	if not params.var_keyword and call.keywords - known:
		return False
	return True


def _balanced(text: str, start: int) -> str | None:
	"""Text from `start` up to the parenthesis that closes the one before it."""
	depth = 1
	quote = ""
	index = start
	while index < len(text):
		char = text[index]
		if quote:
			if char == "\\":
				index += 1
			elif char == quote:
				quote = ""
		elif char in "'\"":
			quote = char
		elif char in "([{":
			depth += 1
		elif char in ")]}":
			depth -= 1
			if depth == 0:
				return text[start:index]
		index += 1
	return None
