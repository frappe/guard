"""Small readers of tree-sitter nodes, for Python and JavaScript. The rules match on them."""

import re
from collections.abc import Iterator

import tree_sitter as ts

CALL = {"python": "call", "javascript": "call_expression"}
FUNCTIONS = {"function_definition", "lambda"}
SPACE = re.compile(r"\s+")


def text(node: ts.Node | None) -> str:
	return (node.text or b"").decode("utf-8", "replace") if node else ""


def compact(node: ts.Node | None) -> str:
	"""The text without white space: `frappe.db .sql` and `frappe.db.sql` are one name."""
	return SPACE.sub("", text(node))


def walk(node: ts.Node, skip: frozenset[str] = frozenset()) -> Iterator[ts.Node]:
	"""Every node under `node`, in source order. Does not go into nodes of a type in `skip`."""
	stack = [node]
	while stack:
		current = stack.pop()
		yield current
		stack.extend(reversed([c for c in current.children if c.type not in skip]))


def of_type(node: ts.Node, *types: str, skip: frozenset[str] = frozenset()) -> Iterator[ts.Node]:
	return (n for n in walk(node, skip) if n.type in types)


def ancestors(node: ts.Node) -> Iterator[ts.Node]:
	parent = node.parent
	while parent is not None:
		yield parent
		parent = parent.parent


def enclosing(node: ts.Node, *types: str) -> ts.Node | None:
	return next((a for a in ancestors(node) if a.type in types), None)


# ------------------------------------------------------------------ calls


def calls(node: ts.Node, language: str = "python", skip: frozenset[str] = frozenset()) -> Iterator[ts.Node]:
	return of_type(node, CALL[language], skip=skip)


def callee(call: ts.Node) -> str:
	"""The name that a call calls, such as `frappe.db.sql` or `frappe.cache().set`."""
	return compact(call.child_by_field_name("function"))


def arguments(call: ts.Node) -> list[ts.Node]:
	"""Positional arguments. Comments and splats are not arguments here."""
	args = call.child_by_field_name("arguments")
	if args is None:
		return []
	skip = {"keyword_argument", "comment", "list_splat", "dictionary_splat", "spread_element"}
	return [c for c in args.named_children if c.type not in skip]


def keywords(call: ts.Node) -> dict[str, ts.Node]:
	"""Keyword arguments of a Python call, by name, to their values."""
	args = call.child_by_field_name("arguments")
	found: dict[str, ts.Node] = {}
	for child in args.named_children if args is not None else []:
		name, value = child.child_by_field_name("name"), child.child_by_field_name("value")
		if child.type == "keyword_argument" and name is not None and value is not None:
			found[text(name)] = value
	return found


def has_splat(call: ts.Node) -> bool:
	"""`**kwargs` or `*args`: the call can pass any argument, so a rule cannot say one is missing."""
	args = call.child_by_field_name("arguments")
	return args is not None and any(c.type in ("list_splat", "dictionary_splat") for c in args.named_children)


# ---------------------------------------------------------------- strings


def string_value(node: ts.Node | None) -> str | None:
	"""The value of a plain string literal: not an f-string, not a template with `${}`. Else None."""
	if node is None or node.type != "string":
		return None
	if any(c.type in ("interpolation", "template_substitution") for c in node.children):
		return None
	parts = [c for c in node.children if c.type in ("string_content", "string_fragment", "escape_sequence")]
	return "".join(text(c) for c in parts)


def is_fstring(node: ts.Node | None) -> bool:
	"""A Python f-string with at least one `{}` in it."""
	if node is None or node.type != "string":
		return False
	return any(c.type == "interpolation" for c in node.children)


def is_literal_text(node: ts.Node | None) -> bool:
	"""A string written in the code: plain, f-string, or template."""
	return node is not None and node.type in ("string", "concatenated_string", "template_string")


def is_format_call(node: ts.Node | None) -> bool:
	"""`"...".format(...)`."""
	if node is None or node.type != "call":
		return False
	function = node.child_by_field_name("function")
	return (
		function is not None
		and function.type == "attribute"
		and text(function.child_by_field_name("attribute")) == "format"
		and is_literal_text(function.child_by_field_name("object"))
	)


def operator(node: ts.Node) -> str:
	return text(node.child_by_field_name("operator"))


def is_percent_format(node: ts.Node | None) -> bool:
	"""`"..." % value`."""
	return (
		node is not None
		and node.type == "binary_operator"
		and operator(node) == "%"
		and is_literal_text(node.child_by_field_name("left"))
	)


def is_translation(node: ts.Node | None, language: str = "python") -> bool:
	"""A call of `_()` in Python, or of `__()` in JavaScript."""
	name = "_" if language == "python" else "__"
	return node is not None and node.type == CALL[language] and callee(node) in (name, f"frappe.{name}")


def first_argument(call: ts.Node) -> ts.Node | None:
	args = arguments(call)
	return args[0] if args else None


# --------------------------------------------------------------- functions


def function_of(node: ts.Node) -> ts.Node | None:
	"""The function that holds `node`: a def or a lambda. None at module or class level."""
	return enclosing(node, *FUNCTIONS)


def decorators(function: ts.Node) -> list[ts.Node]:
	parent = function.parent
	if parent is None or parent.type != "decorated_definition":
		return []
	return [c for c in parent.children if c.type == "decorator"]


def whitelist_decorator(function: ts.Node) -> ts.Node | None:
	"""The `@frappe.whitelist(...)` decorator of a function, if it has one."""
	for decorator in decorators(function):
		name = compact(decorator).removeprefix("@").split("(", 1)[0]
		if name in ("frappe.whitelist", "whitelist"):
			return decorator
	return None


def decorator_keywords(decorator: ts.Node) -> dict[str, ts.Node]:
	call = next((c for c in decorator.named_children if c.type == "call"), None)
	return keywords(call) if call is not None else {}


def class_of(function: ts.Node) -> ts.Node | None:
	"""The class that a function is a method of."""
	parent = function.parent
	if parent is not None and parent.type == "decorated_definition":
		parent = parent.parent
	if parent is None or parent.type != "block":
		return None
	owner = parent.parent
	return owner if owner is not None and owner.type == "class_definition" else None


def name_of(definition: ts.Node) -> str:
	return text(definition.child_by_field_name("name"))


def methods(cls: ts.Node) -> Iterator[ts.Node]:
	body = cls.child_by_field_name("body")
	for child in body.named_children if body is not None else []:
		if child.type == "decorated_definition":
			child = child.child_by_field_name("definition") or child
		if child.type == "function_definition":
			yield child


def is_self_attribute(node: ts.Node | None) -> bool:
	"""`self.name`, one level deep."""
	if node is None or node.type != "attribute":
		return False
	owner = node.child_by_field_name("object")
	return owner is not None and owner.type == "identifier" and text(owner) == "self"


def attribute_name(node: ts.Node) -> str:
	return text(node.child_by_field_name("attribute"))


def assignments(
	node: ts.Node, skip: frozenset[str] = frozenset()
) -> Iterator[tuple[ts.Node, ts.Node, ts.Node]]:
	"""`(statement, target, value)` of each `target = value` under `node`."""
	for assignment in of_type(node, "assignment", skip=skip):
		left, right = assignment.child_by_field_name("left"), assignment.child_by_field_name("right")
		if left is not None and right is not None:
			yield assignment, left, right
