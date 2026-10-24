"""Project rules that code can measure: function complexity, file length and boolean names.

A model counts branches and lines badly, and these rules are exact. Each check runs only when a
rule asks for it, takes its limit from the rule, and reports only what this change made worse.
"""

import ast
import re
from collections.abc import Callable, Iterator

from guard.git.diff import FileDiff
from guard.indexing import languages
from guard.review.findings import Finding
from guard.review.rule_files import ProjectRule

Reader = Callable[[str], str | None]
COMPLEXITY = re.compile(r"complexity\D{0,24}?(\d{1,2})\b", re.I)
FILE_LENGTH = re.compile(r"\bfiles?\b[^.\n]*?\b(\d{2,5})\s+lines\b", re.I)
PREFIX = re.compile(r"`(\w+_)`")
BOOLEAN = re.compile(r"\bbool", re.I)
# A method named for an action that returns whether it worked is not a question, so the boolean
# naming rule does not fit it: `restore`, `copy`, `validate`, `try_copy_clone`.
ACTIONS = {
	"acquire", "add", "apply", "build", "check", "clone", "close", "copy", "create", "delete",
	"ensure", "fetch", "handle", "install", "load", "lock", "make", "open", "parse", "prepare",
	"process", "publish", "read", "register", "release", "remove", "resolve", "restore", "retry",
	"run", "save", "send", "set", "start", "stop", "sync", "try", "update", "validate", "verify",
	"wait", "write",
}  # fmt: skip
FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def findings(files: list[FileDiff], head: Reader, base: Reader, rules: list[ProjectRule]) -> list[Finding]:
	found: list[Finding] = []
	for diff in files:
		if diff.status == "deleted" or diff.is_binary or not diff.added_lines():
			continue
		covering = [rule for rule in rules if rule.covers(diff.path)]
		if not covering:
			continue
		source = head(diff.path) or ""
		old = base(diff.old_path or diff.path) if diff.status != "added" else None
		found += _file_length(diff, source, old, covering)
		if diff.path.endswith(".py"):
			found += _python(diff, source, old, covering)
	return found


# ------------------------------------------------------------------ file length


def _file_length(diff: FileDiff, source: str, old: str | None, rules: list[ProjectRule]) -> list[Finding]:
	found = _limit(FILE_LENGTH, rules)
	if found is None or not languages.detect(diff.path):
		return []
	rule, limit = found
	lines, before = len(source.splitlines()), len(old.splitlines()) if old is not None else 0
	if lines <= limit or before > limit:
		return []
	added = sorted(diff.added_lines())
	line = next((n for n in added if n > limit), added[-1])
	grows = f"grows from {before} to {lines} lines" if before else f"has {lines} lines"
	return [
		_finding(
			diff.path,
			line,
			f"File grows past the {limit}-line limit"
			if before
			else f"New file is over the {limit}-line limit",
			f"`{diff.path}` {grows}.",
			rule,
		)
	]


# ------------------------------------------------------------------ Python


def _python(diff: FileDiff, source: str, old: str | None, rules: list[ProjectRule]) -> list[Finding]:
	tree = _parse(source)
	if tree is None:
		return []
	added = diff.added_lines()
	changed = [
		(name, node) for name, node in _functions(tree) if any(node.lineno <= n <= _end(node) for n in added)
	]
	if not changed:
		return []
	before = dict(_functions(_parse(old) or ast.Module(body=[], type_ignores=[])))
	result = []
	if limit := _limit(COMPLEXITY, rules):
		result += _complexity(diff.path, changed, before, *limit, added)
	if naming_rule := next((r for r in rules if BOOLEAN.search(r.text) and PREFIX.search(r.text)), None):
		result += _boolean_names(diff.path, changed, before, naming_rule)
	return result


def _complexity(
	path: str,
	changed: list[tuple[str, FunctionNode]],
	before: dict[str, FunctionNode],
	rule: ProjectRule,
	limit: int,
	added: set[int],
) -> list[Finding]:
	result = []
	for name, node in changed:
		now = complexity(node)
		old = complexity(before[name]) if name in before else None
		if now <= limit or (old is not None and old >= now):
			continue
		was = f" (was {old})" if old is not None else ""
		# On a line this change added, so the comment lands in the diff and counts as new.
		line = min(n for n in added if node.lineno <= n <= _end(node))
		result.append(
			_finding(
				path,
				line,
				f"`{node.name}` exceeds the complexity limit of {limit}",
				f"`{name}` has cyclomatic complexity {now}{was}, counting `and`/`or` and comprehension conditions.",
				rule,
			)
		)
	return result


def _boolean_names(
	path: str, changed: list[tuple[str, FunctionNode]], before: dict[str, FunctionNode], rule: ProjectRule
) -> list[Finding]:
	prefixes = tuple(PREFIX.findall(rule.text))
	result = []
	for name, node in changed:
		bare = node.name.lstrip("_")
		if name in before or not _returns_bool(node) or bare.startswith(prefixes) or _is_special(node.name):
			continue
		if bare.split("_", 1)[0] in ACTIONS:
			continue
		result.append(
			_finding(
				path,
				node.lineno,
				f"Boolean `{node.name}` does not start with {' or '.join(f'`{p}`' for p in prefixes)}",
				f"`{name}` returns a boolean.",
				rule,
			)
		)
	return result


def complexity(node: FunctionNode) -> int:
	"""McCabe complexity: 1, plus one for each branch, loop, handler, case and extra `and`/`or`
	operand. Nested functions count on their own."""
	total = 1
	for child in _own_nodes(node):
		if isinstance(child, ast.If | ast.For | ast.AsyncFor | ast.While | ast.IfExp | ast.ExceptHandler):
			total += 1
		elif isinstance(child, ast.BoolOp):
			total += len(child.values) - 1
		elif isinstance(child, ast.comprehension):
			total += 1 + len(child.ifs)
		elif isinstance(child, ast.match_case):
			total += 1
	return total


def _own_nodes(node: FunctionNode) -> Iterator[ast.AST]:
	stack: list[ast.AST] = list(ast.iter_child_nodes(node))
	while stack:
		child = stack.pop()
		if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
			continue
		yield child
		stack.extend(ast.iter_child_nodes(child))


def _functions(tree: ast.AST, prefix: str = "") -> Iterator[tuple[str, FunctionNode]]:
	"""(qualified name, node) of each function and method, nested ones too."""
	for child in ast.iter_child_nodes(tree):
		if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
			yield f"{prefix}{child.name}", child
			yield from _functions(child, f"{prefix}{child.name}.")
		elif isinstance(child, ast.ClassDef):
			yield from _functions(child, f"{prefix}{child.name}.")


def _limit(pattern: re.Pattern[str], rules: list[ProjectRule]) -> tuple[ProjectRule, int] | None:
	"""The first rule that the pattern finds a number in, and that number."""
	for rule in rules:
		if match := pattern.search(rule.text):
			return rule, int(match.group(1))
	return None


def _returns_bool(node: FunctionNode) -> bool:
	returns = node.returns
	return (isinstance(returns, ast.Name) and returns.id == "bool") or (
		isinstance(returns, ast.Constant) and returns.value == "bool"
	)


def _is_special(name: str) -> bool:
	return (name.startswith("__") and name.endswith("__")) or name.startswith("test")


def _end(node: ast.AST) -> int:
	return getattr(node, "end_lineno", None) or getattr(node, "lineno", 0)


def _parse(source: str | None) -> ast.Module | None:
	if not source:
		return None
	try:
		return ast.parse(source)
	except SyntaxError:
		return None


def _finding(path: str, line: int, title: str, body: str, rule: ProjectRule) -> Finding:
	return Finding(
		path,
		line,
		line,
		"P2",
		"style",
		title,
		f"{body} `{rule.path}` line {rule.line} asks: {rule.text}",
		category="style",
		confidence=0.95,
		rule_id=rule.id,
		verified=True,
	)
