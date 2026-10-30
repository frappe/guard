"""JavaScript and JSON rules of the Frappe review pack, after frappe/semgrep-rules.

The translation rules of frappe/semgrep-rules also name JSON, but `__()` cannot occur in JSON, so
only `relaxed-permissions` reads JSON here.
"""

import re
from collections.abc import Iterator

import tree_sitter as ts

from guard.review.frappe_pack import syntax as s
from guard.review.frappe_pack.engine import Hit, Source, rule

JS = "javascript"
VARIABLES_ONLY = re.compile(r"^\{\d*\}(\s\{\d*\})*$")
EDGE_SPACE = re.compile(r"^[ \t]+|[ \t]+$")
# `{{ __("Name") }}`: a Jinja template that translates inside itself.
JINJA_TRANSLATION = re.compile(r"\{\{.*_.*\}\}")
# A string continued on the next line with a backslash.
CONTINUED = re.compile(r"\\\s*\n")
MESSAGES = frozenset({"frappe.msgprint", "frappe.throw", "frappe.show_alert"})


def _calls(source: Source) -> Iterator[ts.Node]:
	return s.calls(source.root, JS)


def _translations(source: Source) -> Iterator[tuple[ts.Node, ts.Node]]:
	"""`(call, first argument)` of each `__()`."""
	for call in _calls(source):
		if s.is_translation(call, JS) and (arg := s.first_argument(call)) is not None:
			yield call, arg


def _plain(node: ts.Node | None) -> bool:
	"""A plain string, not one that a Jinja template translates."""
	return node is not None and node.type == "string" and not JINJA_TRANSLATION.search(s.text(node))


def _in_report(path: str) -> bool:
	return "/report/" in f"/{path}"


@rule(
	"frappe-missing-translate-function-js",
	JS,
	"P2",
	"translation",
	"Message not translated",
	"Users see this text, and it is not wrapped in `__()`, so it shows in English in every language.",
	comment_type="style",
)
def untranslated_message(source: Source) -> Iterator[Hit]:
	for call in _calls(source):
		if s.callee(call) in MESSAGES and _plain(message := s.first_argument(call)) and message:
			yield Hit.at(message)


@rule(
	"frappe-missing-translation-button-text",
	JS,
	"P2",
	"translation",
	"Button text not translated",
	"Users see this button label, and it is not wrapped in `__()`.",
	comment_type="style",
)
def untranslated_button(source: Source) -> Iterator[Hit]:
	for call in _calls(source):
		if (
			s.callee(call).endswith("frm.add_custom_button")
			and _plain(label := s.first_argument(call))
			and label
		):
			yield Hit.at(label)


@rule(
	"frappe-translation-empty-string",
	JS,
	"P2",
	"translation",
	"Empty string translated",
	'`__("")` has nothing to translate. Remove the call.',
	comment_type="style",
)
def empty_translation(source: Source) -> Iterator[Hit]:
	yield from (Hit.at(call) for call, arg in _translations(source) if s.string_value(arg) == "")


@rule(
	"frappe-translation-variable-only",
	JS,
	"P2",
	"translation",
	"Only placeholders translated",
	"This text has only placeholders, so translators get nothing to translate. Put the words of the message in the string.",
	comment_type="style",
)
def variable_only(source: Source) -> Iterator[Hit]:
	for call, arg in _translations(source):
		if VARIABLES_ONLY.match(s.string_value(arg) or "-"):
			yield Hit.at(call)


@rule(
	"frappe-translation-trailing-spaces",
	JS,
	"P2",
	"translation",
	"Space at the edge of a translated string",
	"Translators often drop a space at the start or end of a string. Keep the spaces outside `__()`.",
	comment_type="style",
)
def trailing_spaces(source: Source) -> Iterator[Hit]:
	for call, arg in _translations(source):
		if EDGE_SPACE.search(s.string_value(arg) or ""):
			yield Hit.at(call)


@rule(
	"frappe-translation-js-formatting",
	JS,
	"P2",
	"translation",
	"Template string translated",
	'`__()` gets text that already has its values in it, so no translation matches it. Translate the template with placeholders, then pass the values: `__("{{0}} is late", [name])`.',
	comment_type="style",
)
def template_translation(source: Source) -> Iterator[Hit]:
	for call, arg in _translations(source):
		if arg.type == "template_string" and any(c.type == "template_substitution" for c in arg.children):
			yield Hit.at(call)


@rule(
	"frappe-translation-js-splitting",
	JS,
	"P2",
	"translation",
	"Translated text split in parts",
	"Translators see each part alone, and other languages need another word order. Translate the whole sentence in one `__()`, with placeholders for values.",
	comment_type="style",
)
def split_translation(source: Source) -> Iterator[Hit]:
	for node in s.of_type(source.root, "binary_expression"):
		if s.operator(node) != "+":
			continue
		left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
		if s.is_translation(left, JS) and s.is_translation(right, JS):
			yield Hit.at(node)
	for call, arg in _translations(source):
		joined = arg.type == "binary_expression" and s.operator(arg) == "+"
		parts = (arg.child_by_field_name("left"), arg.child_by_field_name("right"))
		if (joined and all(p is not None and p.type == "string" for p in parts)) or CONTINUED.search(
			s.text(arg)
		):
			yield Hit.at(call)


@rule(
	"frappe-translated-values-in-business-logic",
	JS,
	"P2",
	"correctness",
	"Translated filter option",
	'The value of this filter option is translated, so code that compares it must translate too, and it breaks in other languages. Pass `{{label: __("Value"), value: "Value"}}`.',
	paths=_in_report,
)
def translated_options(source: Source) -> Iterator[Hit]:
	for pair in s.of_type(source.root, "pair"):
		if s.text(pair.child_by_field_name("key")) != "options":
			continue
		value = pair.child_by_field_name("value")
		if value is None or value.type != "array":
			continue
		if not any(s.is_translation(item, JS) for item in value.named_children):
			continue
		if any(_key(a) == "filters" for a in s.ancestors(pair) if a.type == "pair"):
			yield Hit.at(pair)


def _key(pair: ts.Node) -> str:
	key = pair.child_by_field_name("key")
	return s.string_value(key) or s.text(key)


@rule(
	"frappe-cur-frm-usage",
	JS,
	"P2",
	"correctness",
	"`cur_frm` is used",
	"`cur_frm` is the form on screen when the code runs, which need not be the form of this script. Use the `frm` that the event handler gets.",
)
def cur_frm(source: Source) -> Iterator[Hit]:
	for node in s.of_type(source.root, "identifier"):
		if s.text(node) == "cur_frm":
			yield Hit.at(node)


@rule(
	"frappe-incorrect-debounce",
	JS,
	"P1",
	"correctness",
	"Debounce made on each call",
	"This makes a new debounced function each time it runs, so no call waits for another and nothing is debounced. Make it once, then call the result.",
)
def incorrect_debounce(source: Source) -> Iterator[Hit]:
	for call in _calls(source):
		inner = call.child_by_field_name("function")
		if (
			inner is not None
			and inner.type == "call_expression"
			and s.callee(inner) == "frappe.utils.debounce"
		):
			yield Hit.at(call)


@rule(
	"use-vanilla-js-include",
	JS,
	"P2",
	"maintainability",
	"`in_list` is used",
	"`in_list(list, item)` wraps `list.includes(item)`. Call `includes`.",
	on=False,
	comment_type="style",
)
def in_list(source: Source) -> Iterator[Hit]:
	for call in _calls(source):
		if s.callee(call) == "in_list" and len(s.arguments(call)) == 2:
			yield Hit.at(call)


@rule(
	"relaxed-permissions",
	"json",
	"P2",
	"security",
	"Permission for the role All",
	"Every user has the role All, website users too. Give this permission to a role that only the right users have.",
)
def relaxed_permissions(source: Source) -> Iterator[Hit]:
	for node in s.of_type(source.root, "object"):
		for pair in (c for c in node.named_children if c.type == "pair"):
			if (
				s.string_value(pair.child_by_field_name("key")) == "role"
				and s.string_value(pair.child_by_field_name("value")) == "All"
			):
				yield Hit.at(pair)
