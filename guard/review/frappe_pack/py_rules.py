"""Python rules of the Frappe review pack, after frappe/semgrep-rules.

Each rule keeps the id of its Semgrep rule, so that `# nosemgrep: <id>` covers both. Where Semgrep
flags code that a person must audit, the rule is off until a config turns it on. A few rules read
more context than Semgrep does, because Guard comments on pull requests and its noise costs trust:
see the docstring of each.
"""

import json
import re
from collections.abc import Iterator

import tree_sitter as ts

from guard.review.frappe_pack import syntax as s
from guard.review.frappe_pack.engine import Hit, Source, line_of, rule

# Methods of a document that Frappe runs while a request saves, submits or deletes it.
DOC_HOOKS = frozenset(
	{
		"before_validate",
		"validate",
		"before_insert",
		"after_insert",
		"before_save",
		"on_update",
		"before_submit",
		"on_submit",
		"before_cancel",
		"on_cancel",
		"before_update_after_submit",
		"on_update_after_submit",
		"on_change",
		"on_trash",
		"after_delete",
		"before_rename",
		"after_rename",
	}
)
# Hooks that run after Frappe wrote the document: a change to `self` there is not saved.
AFTER_SAVE_HOOKS = frozenset(
	{"on_submit", "on_cancel", "after_insert", "on_update", "on_update_after_submit"}
)
# A call that checks who the caller is: frappe.has_permission, doc.check_permission,
# frappe.only_for, and helpers of the app such as require_admin() or drive_check().
ACCESS_CHECK = re.compile(r"\b\w*(?:permission|require|_check|authori[sz]|only_for|has_role)\w*\s*\(", re.I)
COMMENT_LINES = 3  # lines above a call that its comment can take
SAFE_METHODS = re.compile(r"\b(GET|HEAD|OPTIONS|QUERY)\b")
# Calls that write outside the transaction, so the rollback of a GET request does not undo them.
GET_SIDE_EFFECTS = frozenset(
	{
		"frappe.db.commit",
		"frappe.sendmail",
		"frappe.db.sql_ddl",
		"frappe.db.add_index",
		"frappe.db.add_unique",
		"frappe.db.truncate",
		"frappe.db.rename_table",
		"frappe.db.change_column_type",
	}
)
ENQUEUE = frozenset({"frappe.enqueue", "frappe.enqueue_doc"})
# Values that belong to one site. A module variable keeps the value of the first site that a worker
# served, and gives it to the next one.
SITE_CALLS = frozenset(
	{
		"frappe.get_all",
		"frappe.get_list",
		"frappe.get_doc",
		"frappe.get_single",
		"frappe.get_cached_doc",
		"frappe.get_meta",
		"frappe.get_hooks",
		"frappe._",
		"flt",
	}
)
SITE_ATTRIBUTES = frozenset({"frappe.cache", "frappe.conf", "frappe.flags", "frappe.lang", "frappe.qb"})
LOCAL_PROXIES = frozenset(
	{
		"db",
		"qb",
		"conf",
		"form",
		"form_dict",
		"request",
		"response",
		"session",
		"user",
		"flags",
		"error_log",
		"debug_log",
		"message_log",
		"lang",
	}
)
SORT_ORDERS = re.compile(r"^(['\"](asc|desc)['\"]|frappe\.qb\.(asc|desc)|Order\.(asc|desc))$", re.I)
VARIABLES_ONLY = re.compile(r"^\{\d*\}(\s\{\d*\})*$")
EDGE_SPACE = re.compile(r"^[ \t]+|[ \t]+$")
# Columns that every DocType has, besides its fields.
STANDARD_FIELDS = frozenset({"owner", "docstatus", "idx", "parent", "parentfield", "parenttype"})
NOT_SAVED = re.compile(r"^(ignore_linked_doctypes|status_updater)")
CODE_CALLS = frozenset({"eval", "exec", "safe_exec", "safe_eval", "get_attr", "frappe.call"})
# Names that Frappe code imports to set them: `from frappe import local` then `local.flags.x = 1`.
PROXY_IMPORTS = frozenset({"local", "flags"})
NO_FUNCTIONS = frozenset(s.FUNCTIONS)
STATEMENT_PARENTS = frozenset({"module", "block", "expression_statement"})
COMMENT = re.compile(r"(?:^|\s)#")
# `{{ _("Name") }}`: a Jinja template that translates inside itself.
JINJA_TRANSLATION = re.compile(r"\{\{.*_.*\}\}")


def under(*parts: str):
	"""A path filter: the file is in a folder with one of these names."""
	return lambda path: any(f"/{part}/" in f"/{path}" for part in parts)


def named(*names: str):
	return lambda path: path.rsplit("/", 1)[-1] in names


def is_test_module(path: str) -> bool:
	name = path.rsplit("/", 1)[-1]
	return (name.startswith("test_") and name.endswith(".py")) or path.endswith(
		"frappe/tests/ui_test_helpers.py"
	)


def _calls(source: Source, *names: str) -> Iterator[ts.Node]:
	return (c for c in s.calls(source.root) if s.callee(c) in names)


def _serves_request(function: ts.Node) -> bool:
	"""A whitelisted method, or a document hook: Frappe runs it in a request."""
	if s.whitelist_decorator(function) is not None:
		return True
	return s.class_of(function) is not None and s.name_of(function) in DOC_HOOKS


def _whitelisted(source: Source) -> Iterator[tuple[ts.Node, ts.Node]]:
	"""`(function, decorator)` of each whitelisted function."""
	for function in s.of_type(source.root, "function_definition"):
		decorator = s.whitelist_decorator(function)
		if decorator is not None:
			yield function, decorator


def _commented(source: Source, line: int) -> bool:
	"""A comment on the line, or a comment line just above it."""
	index = line - 1
	if COMMENT.search(source.lines[index]):
		return True
	return any(text.lstrip().startswith("#") for text in source.lines[max(index - COMMENT_LINES, 0) : index])


def _built_string(node: ts.Node | None) -> bool:
	"""Text built from values: an f-string, `"...".format()`, `"..." % x`, or `"..." + x`."""
	if node is None:
		return False
	if s.is_fstring(node) or s.is_format_call(node):
		return True
	if s.is_percent_format(node):
		return not s.is_literal_text(node.child_by_field_name("right"))
	if node.type == "binary_operator" and s.operator(node) == "+":
		left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
		literals = [s.is_literal_text(left), s.is_literal_text(right)]
		return (any(literals) and not all(literals)) or _built_string(left) or _built_string(right)
	return False


def _assigned_before(node: ts.Node, name: str) -> ts.Node | None:
	"""The last `name = value` before `node`, in its function or module. Returns the statement."""
	scope = s.function_of(node) or _module(node)
	last = None
	for statement, target, _value in s.assignments(scope, skip=NO_FUNCTIONS):
		if statement.start_byte >= node.start_byte:
			break
		if target.type == "identifier" and s.text(target) == name:
			last = statement
	return last


def _module(node: ts.Node) -> ts.Node:
	root = node
	while root.parent is not None:
		root = root.parent
	return root


def _value_of(statement: ts.Node) -> ts.Node | None:
	return statement.child_by_field_name("right")


# -------------------------------------------------------------- security


@rule(
	"frappe-sql-format-injection",
	"python",
	"P1",
	"security",
	"SQL built from strings",
	"This query is built with string formatting. Pass values as parameters to `frappe.db.sql` so input cannot change the SQL.",
)
def sql_format_injection(source: Source) -> Iterator[Hit]:
	for call in _calls(source, "frappe.db.sql", "frappe.db.multisql"):
		query = s.first_argument(call)
		if _built_string(query):
			yield Hit.at(call)
		elif query is not None and query.type == "identifier":
			statement = _assigned_before(call, s.text(query))
			if statement is not None and _built_string(_value_of(statement)):
				yield Hit.at(call, statement)


@rule(
	"guest-whitelisted-method",
	"python",
	"P1",
	"security",
	"New guest endpoint",
	"This endpoint is open to anyone without a login. Check that it exposes no records and validates every argument.",
)
def guest_whitelisted_method(source: Source) -> Iterator[Hit]:
	"""Semgrep flags every guest endpoint. Guard flags one whose body calls no access check."""
	for function, decorator in _whitelisted(source):
		guest = s.decorator_keywords(decorator).get("allow_guest")
		if s.text(guest) != "True":
			continue
		if not ACCESS_CHECK.search(s.text(function.child_by_field_name("body"))):
			yield Hit.at(decorator)


@rule(
	"frappe-ignore-permissions",
	"python",
	"P2",
	"security",
	"Permission check is bypassed",
	"`ignore_permissions=True` skips the role checks. Confirm the caller is already checked, or remove it.",
)
def ignore_permissions(source: Source) -> Iterator[Hit]:
	"""Not in frappe/semgrep-rules. A bypass needs a comment that explains it. Only a whitelisted
	method lets a caller reach it, and a check of the caller before the bypass makes it safe."""
	bypasses = [
		node
		for node in s.of_type(source.root, "keyword_argument")
		if s.text(node.child_by_field_name("name")) == "ignore_permissions"
		and s.text(node.child_by_field_name("value")) == "True"
	]
	bypasses += [
		statement
		for statement, target, value in s.assignments(source.root)
		if s.compact(target).endswith("ignore_permissions") and s.text(value) == "True"
	]
	for node in bypasses:
		function = s.enclosing(node, "function_definition")
		if function is None or s.whitelist_decorator(function) is None:
			continue
		body = function.child_by_field_name("body")
		before = source.text.encode()[body.start_byte if body else 0 : node.start_byte].decode(
			"utf-8", "replace"
		)
		if not ACCESS_CHECK.search(before) and not _commented(source, line_of(node)):
			yield Hit.at(node)


@rule(
	"unchecked-frappe-permission-call",
	"python",
	"P1",
	"security",
	"Permission check result is ignored",
	"`frappe.has_permission` returns True or False. It stops nothing unless it gets `throw=True`, and this code ignores what it returns.",
)
def unchecked_permission(source: Source) -> Iterator[Hit]:
	for call in _calls(source, "frappe.has_permission"):
		parent = call.parent
		# The grammar has no expression statement: a call that is a statement sits in its block.
		if parent is not None and parent.type in STATEMENT_PARENTS and "throw" not in s.keywords(call):
			yield Hit.at(call)


@rule(
	"whitelisted-side-effect-on-get",
	"python",
	"P1",
	"security",
	"Change on a GET request",
	'`{function}` accepts GET requests, which skip the CSRF check. Frappe rolls back a GET request at its end, but `{call}` escapes that rollback. Set `methods=["POST"]` on `@frappe.whitelist`.',
)
def side_effect_on_get(source: Source) -> Iterator[Hit]:
	for function, decorator in _whitelisted(source):
		methods = s.decorator_keywords(decorator).get("methods")
		if methods is not None and not SAFE_METHODS.search(s.text(methods)):
			continue
		for call in s.calls(function):
			name = s.callee(call)
			if name in GET_SIDE_EFFECTS or (name in ENQUEUE and _escapes_rollback(call)):
				yield Hit.at(call, decorator, function=s.name_of(function), call=name)


def _escapes_rollback(call: ts.Node) -> bool:
	"""A job that starts now, or one that is not held for the commit: the rollback cannot stop it."""
	options = s.keywords(call)
	after_commit = s.text(options.get("enqueue_after_commit")) == "True"
	return not after_commit or s.text(options.get("now")) == "True"


@rule(
	"frappe-format-string-injection",
	"python",
	"P1",
	"security",
	"Exception passed to a translated message",
	"`{name}` is an exception object. A translation can read its attributes through format fields such as `{{0.__class__}}`. Pass `str({name})`.",
)
def format_string_injection(source: Source) -> Iterator[Hit]:
	for call in s.calls(source.root):
		function = call.child_by_field_name("function")
		if function is None or function.type != "attribute" or s.attribute_name(function) != "format":
			continue
		if not s.is_translation(function.child_by_field_name("object")):
			continue
		caught = _caught_names(call)
		for arg in s.arguments(call):
			if arg.type == "identifier" and s.text(arg) in caught:
				yield Hit.at(arg, name=s.text(arg))


def _caught_names(node: ts.Node) -> set[str]:
	"""Names that `except ... as name` binds around `node`."""
	names = set()
	for clause in s.ancestors(node):
		if clause.type != "except_clause":
			continue
		for alias in s.of_type(clause, "as_pattern_target"):
			names.add(s.text(alias))
		for pattern in s.of_type(clause, "as_pattern"):
			alias = pattern.child_by_field_name("alias")
			if alias is not None:
				names.add(s.text(alias))
	return names


@rule(
	"frappe-test-whitelist-missing-protection",
	"python",
	"P1",
	"security",
	"Test endpoint open outside tests",
	"A test module whitelists this method with `@frappe.whitelist()`, so it is open on every site. Use `@whitelist_for_tests()`: it works only in test mode.",
	paths=is_test_module,
	tests=True,
)
def test_whitelist(source: Source) -> Iterator[Hit]:
	for function, decorator in _whitelisted(source):
		if not any("whitelist_for_tests" in s.text(d) for d in s.decorators(function)):
			yield Hit.at(decorator)


@rule(
	"frappe-setuser",
	"python",
	"P1",
	"security",
	"Code runs as another user",
	"`frappe.set_user` changes whose permissions the rest of the request has. Check that the caller cannot reach it with input that they choose, and that the code sets the user back.",
	paths=lambda path: not under("patches")(path),
	# Jobs, install hooks and scripts set the user on purpose. Semgrep asks for an audit, not a fix.
	on=False,
)
def set_user(source: Source) -> Iterator[Hit]:
	yield from (Hit.at(call) for call in _calls(source, "frappe.set_user"))


@rule(
	"frappe-security-file-traversal",
	"python",
	"P2",
	"security",
	"File system access",
	"`open()` reads or writes a file. If any part of the path comes from input, check that it cannot leave the folder that it must stay in.",
	on=False,
)
def file_traversal(source: Source) -> Iterator[Hit]:
	yield from (Hit.at(call) for call in _calls(source, "open"))


@rule(
	"frappe-codeinjection-eval",
	"python",
	"P1",
	"security",
	"Dynamic code runs",
	"`{call}` runs code or calls a method by name. Check that no input can choose the code or the method.",
	on=False,
)
def code_injection(source: Source) -> Iterator[Hit]:
	for call in s.calls(source.root):
		if (name := s.callee(call)) in CODE_CALLS:
			yield Hit.at(call, call=name)


@rule(
	"frappe-ssti",
	"python",
	"P1",
	"security",
	"Template from a variable",
	"`render_template` renders `{template}`. If a user can write that template, they can run code in it. Check that it comes from a trusted source.",
	on=False,
)
def server_side_template(source: Source) -> Iterator[Hit]:
	for call in _calls(source, "render_template", "frappe.render_template"):
		template = s.first_argument(call)
		if template is not None:
			yield Hit.at(call, template=s.text(template))


# ----------------------------------------------------------- correctness


@rule(
	"frappe-manual-commit",
	"python",
	"P2",
	"correctness",
	"Explicit commit",
	"Frappe commits at the end of the request. An explicit commit here can save half-finished work when a later step fails.",
	paths=lambda path: not under("patches", "demo")(path),
)
def manual_commit(source: Source) -> Iterator[Hit]:
	"""Semgrep flags every commit outside a `try`. Guard flags one in a request: in a whitelisted method
	or a document hook. Patches, jobs and scripts commit on purpose."""
	for call in _calls(source, "frappe.db.commit"):
		function = s.enclosing(call, "function_definition")
		if function is not None and _serves_request(function):
			yield Hit.at(call)


@rule(
	"frappe-breaks-multitenancy",
	"python",
	"P1",
	"correctness",
	"Site data in a module variable",
	"`{name}` is set once, when Python imports the module. A worker serves many sites, so the next site gets the value of the first. Read it in a function.",
)
def breaks_multitenancy(source: Source) -> Iterator[Hit]:
	for statement, target, value in s.assignments(source.root, skip=NO_FUNCTIONS):
		if s.function_of(statement) is None and _reads_site(value):
			yield Hit.at(statement, name=s.text(target))


def _reads_site(node: ts.Node) -> bool:
	for part in s.walk(node, skip=NO_FUNCTIONS):
		if part.type == "attribute":
			name = s.compact(part)
			owner = part.child_by_field_name("object")
			if name in SITE_ATTRIBUTES or s.compact(owner) == "frappe.local":
				return True
		elif part.type == "call":
			name = s.callee(part)
			if name in SITE_CALLS or name.startswith("frappe.db."):
				return True
	return False


@rule(
	"frappe-cache-breaks-multitenancy",
	"python",
	"P1",
	"correctness",
	"Cache key without the site",
	"`{call}` reads or writes a Redis key that has no site prefix, so all sites on the server share it. Use `set_value` and `get_value`.",
)
def cache_breaks_multitenancy(source: Source) -> Iterator[Hit]:
	direct = {f"{cache}.{op}" for cache in ("frappe.cache()", "frappe.cache") for op in ("get", "set")}
	for call in s.calls(source.root):
		name = s.callee(call)
		if name in direct:
			yield Hit.at(call, call=name)
			continue
		owner, _, op = name.rpartition(".")
		if op in ("get", "set") and owner.isidentifier():
			statement = _assigned_before(call, owner)
			if statement is not None and s.compact(_value_of(statement)) == "frappe.cache()":
				yield Hit.at(call, call=name)


@rule(
	"frappe-modifying-but-not-comitting",
	"python",
	"P1",
	"correctness",
	"Change after save is lost",
	'`{method}` runs after Frappe saved the document, so `self.{name}` is not saved. Use `self.db_set("{name}", ...)`, or set it in `validate`.',
)
def modified_not_committed(source: Source) -> Iterator[Hit]:
	"""Semgrep flags every attribute. Guard flags the fields of the DocType when it can read its JSON,
	else every attribute that is not private: an attribute such as `self.asset_doc` is not saved, and
	need not be."""
	fields = _fields(source)
	for function in s.of_type(source.root, "function_definition"):
		cls = s.class_of(function)
		if cls is not None and _controller(cls, fields) and s.name_of(function) in AFTER_SAVE_HOOKS:
			for statement, name in _unsaved(function, fields):
				yield Hit.at(statement, method=s.name_of(function), name=name)


@rule(
	"frappe-modifying-but-not-comitting-other-method",
	"python",
	"P2",
	"correctness",
	"Change after save may be lost",
	"`{hook}` calls `{method}`, which sets `self.{name}`. `{hook}` runs after Frappe saved the document, so this change is not saved. Use `self.db_set`.",
	on=False,
)
def modified_in_other_method(source: Source) -> Iterator[Hit]:
	fields = _fields(source)
	for cls in (c for c in s.of_type(source.root, "class_definition") if _controller(c, fields)):
		methods = {s.name_of(m): m for m in s.methods(cls)}
		for hook in (m for name, m in methods.items() if name in AFTER_SAVE_HOOKS):
			if any(s.callee(c) == "self.save" for c in s.calls(hook)):
				continue
			called = {
				s.callee(c).removeprefix("self.") for c in s.calls(hook) if s.callee(c).startswith("self.")
			}
			for name in sorted(called & methods.keys() - AFTER_SAVE_HOOKS):
				for statement, attribute in _unsaved(methods[name], fields):
					yield Hit.at(statement, hook=s.name_of(hook), method=name, name=attribute)


def _controller(cls: ts.Node, fields: set[str] | None) -> bool:
	"""A class with a DocType JSON beside it, or one that extends `Document` or another controller."""
	if fields is not None:
		return True
	bases = s.text(cls.child_by_field_name("superclasses"))
	return bool(re.search(r"\b(\w*Document|\w*Controller|TransactionBase|StatusUpdater)\b", bases))


def _fields(source: Source) -> set[str] | None:
	"""The field names of the DocType of a controller, from the JSON beside it. None when there is none."""
	folder, _, name = source.path.rpartition("/")
	if not name.endswith(".py") or not folder.endswith(f"/{name[:-3]}"):
		return None
	try:
		meta = json.loads(source.read(f"{folder}/{name[:-3]}.json") or "")
	except ValueError:
		return None
	if not isinstance(meta, dict) or not isinstance(meta.get("fields"), list):
		return None
	return {str(f.get("fieldname")) for f in meta["fields"] if isinstance(f, dict)} | STANDARD_FIELDS


def _unsaved(function: ts.Node, fields: set[str] | None) -> Iterator[tuple[ts.Node, str]]:
	"""`self.x = value` in a method, with no `db_set`, `db_update` or `save` after it. Only fields,
	when the fields are known, else attributes that are not private."""
	writes = [
		c
		for c in s.calls(function, skip=NO_FUNCTIONS)
		if s.callee(c) in ("self.db_set", "self.db_update", "self.save")
	]
	for statement, target, value in s.assignments(
		function.child_by_field_name("body") or function, skip=NO_FUNCTIONS
	):
		if not s.is_self_attribute(target):
			continue
		name = s.attribute_name(target)
		if NOT_SAVED.match(name) or (name not in fields if fields is not None else name.startswith("_")):
			continue
		later = [c for c in writes if c.start_byte > statement.end_byte]
		if not any(_saves(c, name, value) for c in later):
			yield statement, name


def _saves(call: ts.Node, name: str, value: ts.Node) -> bool:
	if s.callee(call) != "self.db_set":
		return True
	args = s.text(call.child_by_field_name("arguments"))
	# The value counts only when it is a name: `1` or `True` is in many calls that do not save it.
	named = value.type in ("identifier", "attribute") and re.search(rf"\b{re.escape(s.text(value))}\b", args)
	return f"self.{name}" in args or bool(named) or re.search(rf"['\"]{name}['\"]", args) is not None


@rule(
	"frappe-after-save-controller-hook",
	"python",
	"P1",
	"correctness",
	"`after_save` never runs",
	"Frappe has no `after_save` hook, so this method never runs. Use `on_update`.",
	paths=under("doctype"),
)
def after_save_hook(source: Source) -> Iterator[Hit]:
	for cls in s.of_type(source.root, "class_definition"):
		if cls.child_by_field_name("superclasses") is None:
			continue
		for method in s.methods(cls):
			name = method.child_by_field_name("name")
			if name is not None and s.text(name) == "after_save":
				yield Hit.at(name)


@rule(
	"frappe-modifying-child-tables-while-iterating",
	"python",
	"P1",
	"correctness",
	"Child table changes while a loop reads it",
	"This loop reads `self.{table}` and changes a child table in its body. A changed list makes the loop skip or repeat rows. Loop over a copy, or collect the changes and apply them after the loop.",
	paths=under("doctype"),
)
def child_table_while_iterating(source: Source) -> Iterator[Hit]:
	for loop in s.of_type(source.root, "for_statement"):
		table = loop.child_by_field_name("right")
		body = loop.child_by_field_name("body")
		if table is None or body is None or not s.is_self_attribute(table):
			continue
		for call in s.calls(body, skip=NO_FUNCTIONS):
			if s.callee(call) in ("self.remove", "self.append"):
				yield Hit.at(call, table=s.attribute_name(table))


@rule(
	"frappe-same-key-assigned-twice",
	"python",
	"P1",
	"correctness",
	"Key set twice",
	"This dictionary sets `{key}` twice. Python keeps the last value, so the first one is lost.",
)
def same_key_twice(source: Source) -> Iterator[Hit]:
	for dictionary in s.of_type(source.root, "dictionary"):
		seen: set[str] = set()
		for pair in (c for c in dictionary.named_children if c.type == "pair"):
			key = _key(pair.child_by_field_name("key"))
			if key in seen:
				yield Hit.at(pair, key=key)
			seen.add(key)
	for call in _calls(source, "dict", "_dict", "frappe._dict"):
		seen = set()
		for item in s.arguments(call):
			if item.type == "tuple" and len(item.named_children) == 2:
				key = _key(item.named_children[0])
				if key in seen:
					yield Hit.at(item, key=key)
				seen.add(key)


def _key(node: ts.Node | None) -> str:
	"""`"a"` and `'a'` are one key. White space in a string is part of the key."""
	value = s.string_value(node)
	return repr(value) if value is not None else s.compact(node)


@rule(
	"frappe-redis-flush",
	"python",
	"P1",
	"correctness",
	"Redis flushall",
	"`flushall` empties every database on the Redis server, for all sites and benches on it. Use `flushdb`, or delete the keys of this site.",
)
def redis_flush(source: Source) -> Iterator[Hit]:
	yield from (Hit.at(c) for c in _calls(source, "frappe.cache.flushall", "frappe.cache().flushall"))


@rule(
	"frappe-overriding-local-proxies",
	"python",
	"P1",
	"correctness",
	"Local proxy replaced",
	"`frappe.{name}` is a proxy to the value of the current request. This assignment replaces the proxy for every later request of the worker. Set `frappe.local.{name}`.",
)
def overriding_local_proxies(source: Source) -> Iterator[Hit]:
	for statement, target, _value in s.assignments(source.root):
		if target.type != "attribute" or s.compact(target.child_by_field_name("object")) != "frappe":
			continue
		if (name := s.attribute_name(target)) in LOCAL_PROXIES:
			yield Hit.at(statement, name=name)


@rule(
	"frappe-single-value-type-safety",
	"python",
	"P2",
	"correctness",
	"`get_value` on a single DocType",
	"For a single DocType, `frappe.db.get_value` returns the value as text. `frappe.db.get_single_value` returns it with its field type.",
)
def single_value(source: Source) -> Iterator[Hit]:
	for call in _calls(source, "frappe.db.get_value"):
		args = s.arguments(call)
		if len(args) < 2 or not _single_name(args[0], args[1]):
			continue
		if len(args) > 2 and args[2].type in ("list", "tuple"):
			continue
		yield Hit.at(call)


@rule(
	"frappe-set-value-semantics",
	"python",
	"P2",
	"correctness",
	"`set_value` on a single DocType",
	"For a single DocType, use `frappe.db.set_single_value`. It says what the code does, and it does not depend on the name of the record.",
)
def set_value(source: Source) -> Iterator[Hit]:
	for call in _calls(source, "frappe.db.set_value"):
		args = s.arguments(call)
		if len(args) >= 2 and _single_name(args[0], args[1]):
			yield Hit.at(call)


def _single_name(doctype: ts.Node, name: ts.Node) -> bool:
	return name.type == "none" or s.compact(name) == s.compact(doctype)


@rule(
	"frappe-qb-incorrect-order-usage",
	"python",
	"P1",
	"correctness",
	"Sort order passed as a field",
	"`orderby` reads every positional argument as a field, so this query sorts by a field named `{order}`, in the default order. Pass `order=frappe.qb.desc`.",
)
def qb_order(source: Source) -> Iterator[Hit]:
	for call in s.calls(source.root):
		if not s.callee(call).endswith(".orderby"):
			continue
		args = s.arguments(call)
		if len(args) >= 2 and SORT_ORDERS.match(s.compact(args[-1])):
			yield Hit.at(call, order=s.text(args[-1]).strip("'\""))


@rule(
	"frappe-enqueue-without-after-commit",
	"python",
	"P2",
	"correctness",
	"Job can start before the commit",
	"A controller enqueues this job. It can start before Frappe commits this transaction, and then read old data or no document. Pass `enqueue_after_commit=True`.",
	paths=under("doctype"),
)
def enqueue_without_after_commit(source: Source) -> Iterator[Hit]:
	"""Semgrep reads every class. Guard reads DocType controllers, which is what its message is about."""
	for call in _calls(source, "frappe.enqueue", "frappe.enqueue_doc", "enqueue", "enqueue_doc"):
		function = s.enclosing(call, "function_definition")
		if function is None or s.class_of(function) is None or s.has_splat(call):
			continue
		options = s.keywords(call)
		if "enqueue_after_commit" in options or s.text(options.get("now")) == "True":
			continue
		if s.text(options.get("is_async")) == "False":
			continue
		yield Hit.at(call)


@rule(
	"frappe-realtime-pick-room",
	"python",
	"P1",
	"security",
	"Realtime message to every user",
	"This message goes to every user of the site. Pass `user`, `doctype` and `docname`, or `room`, so that only those who may see it get it.",
)
def realtime_room(source: Source) -> Iterator[Hit]:
	for call in _calls(source, "frappe.publish_realtime"):
		# The third positional argument is the room, and the fourth the user.
		if s.has_splat(call) or len(s.arguments(call)) >= 3:
			continue
		if not {"doctype", "docname", "room", "user"} & s.keywords(call).keys():
			yield Hit.at(call)


@rule(
	"frappe-monkey-patching-not-allowed",
	"python",
	"P2",
	"correctness",
	"Monkey patch",
	"This changes `{name}` of an imported module while the app runs. Every site on the bench gets the change, and an update of that module can break it. Use a hook of the framework.",
)
def monkey_patching(source: Source) -> Iterator[Hit]:
	imported = _from_imports(source.root)
	for statement, target, _value in s.assignments(source.root):
		if target.type != "attribute":
			continue
		root = target
		while root.type == "attribute":
			root = root.child_by_field_name("object") or root
			if root.type not in ("attribute", "identifier"):
				break
		if root.type == "identifier" and s.text(root) in imported:
			yield Hit.at(statement, name=s.compact(target))


def _from_imports(root: ts.Node) -> set[str]:
	names: set[str] = set()
	for statement in s.of_type(root, "import_from_statement"):
		module = s.compact(statement.child_by_field_name("module_name"))
		for name in statement.children_by_field_name("name"):
			alias = name.child_by_field_name("alias") if name.type == "aliased_import" else name
			bound = s.text(alias)
			if not (module == "frappe" and bound in PROXY_IMPORTS) and "." not in bound:
				names.add(bound)
	return names


@rule(
	"frappe-query-debug-statement",
	"python",
	"P2",
	"correctness",
	"Debug output left on",
	"`debug=True` prints the query and its timing on every call. Remove it before merge.",
)
def debug_statement(source: Source) -> Iterator[Hit]:
	for call in s.calls(source.root):
		debug = s.keywords(call).get("debug")
		if s.text(debug) in ("True", "1"):
			yield Hit.at(debug or call)


@rule(
	"override-doctype-class",
	"python",
	"P2",
	"correctness",
	"`override_doctype_class` in hooks",
	"`override_doctype_class` replaces the controller of a DocType. Only one app can do it, and it breaks without a word when the original controller changes. Use `doc_events` or `extend_doctype_class`.",
	paths=named("hooks.py"),
)
def override_doctype_class(source: Source) -> Iterator[Hit]:
	for statement, target, _value in s.assignments(source.root, skip=NO_FUNCTIONS):
		if s.text(target) == "override_doctype_class" and s.function_of(statement) is None:
			yield Hit.at(statement)


# ------------------------------------------------------------ translation


def _translations(source: Source) -> Iterator[tuple[ts.Node, ts.Node]]:
	"""`(call, first argument)` of each `_()`."""
	for call in s.calls(source.root):
		if s.is_translation(call) and (arg := s.first_argument(call)) is not None:
			yield call, arg


@rule(
	"frappe-translation-empty-string",
	"python",
	"P2",
	"translation",
	"Empty string translated",
	'`_("")` has nothing to translate. Remove the call.',
	comment_type="style",
)
def empty_translation(source: Source) -> Iterator[Hit]:
	yield from (Hit.at(call) for call, arg in _translations(source) if s.string_value(arg) == "")


@rule(
	"frappe-translation-variable-only",
	"python",
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
	"python",
	"P2",
	"translation",
	"Space at the edge of a translated string",
	"Translators often drop a space at the start or end of a string. Keep the spaces outside `_()`.",
	comment_type="style",
)
def trailing_spaces(source: Source) -> Iterator[Hit]:
	for call, arg in _translations(source):
		if EDGE_SPACE.search(s.string_value(arg) or ""):
			yield Hit.at(call)


@rule(
	"frappe-translation-python-formatting",
	"python",
	"P2",
	"translation",
	"Text formatted before translation",
	'`_()` gets text that already has its values in it, so no translation matches it. Translate the template with positional placeholders, then format it: `_("{{0}} is late").format(name)`.',
	comment_type="style",
)
def formatted_before_translation(source: Source) -> Iterator[Hit]:
	for call, arg in _translations(source):
		if s.is_fstring(arg) or s.is_format_call(arg) or s.is_percent_format(arg):
			yield Hit.at(call)
		elif arg.type == "identifier" and (statement := _assigned_before(call, s.text(arg))) is not None:
			value = _value_of(statement)
			if s.is_format_call(value) or s.is_percent_format(value) or s.is_fstring(value):
				yield Hit.at(call, statement)


@rule(
	"frappe-translation-python-splitting",
	"python",
	"P2",
	"translation",
	"Translated text split in parts",
	"Translators see each part alone, and other languages need another word order. Translate the whole sentence in one `_()`, with placeholders for values.",
	comment_type="style",
)
def split_translation(source: Source) -> Iterator[Hit]:
	for node in s.of_type(source.root, "binary_operator"):
		if s.operator(node) != "+":
			continue
		left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
		if s.is_translation(left) and s.is_translation(right):
			yield Hit.at(node)
	for call, arg in _translations(source):
		joined = arg.type == "binary_operator" and s.operator(arg) == "+"
		if joined and all(
			s.is_literal_text(c) for c in (arg.child_by_field_name("left"), arg.child_by_field_name("right"))
		):
			yield Hit.at(call)
		elif "\\\n" in s.text(arg):
			yield Hit.at(call)


@rule(
	"frappe-untranslated-label-in-translated-message",
	"python",
	"P2",
	"translation",
	"Label not translated",
	"`self.meta.get_label()` returns the label in English. Wrap it in `_()` before it goes into the translated message.",
	comment_type="style",
)
def untranslated_label(source: Source) -> Iterator[Hit]:
	for call in s.calls(source.root):
		function = call.child_by_field_name("function")
		if function is None or function.type != "attribute" or s.attribute_name(function) != "format":
			continue
		if not s.is_translation(function.child_by_field_name("object")):
			continue
		for arg in s.arguments(call):
			if arg.type == "call" and s.callee(arg) == "self.meta.get_label":
				yield Hit.at(arg)


@rule(
	"frappe-missing-translate-function-python",
	"python",
	"P2",
	"translation",
	"Message not translated",
	"Users see this text, and it is not wrapped in `_()`, so it shows in English in every language.",
	comment_type="style",
)
def untranslated_message(source: Source) -> Iterator[Hit]:
	for call in _calls(source, "frappe.msgprint", "frappe.throw"):
		message = s.first_argument(call)
		if message is not None and message.type == "binary_operator" and s.operator(message) == "+":
			message = message.child_by_field_name("left")
		if message is not None and message.type == "string" and not JINJA_TRANSLATION.search(s.text(message)):
			yield Hit.at(message)
		title = s.keywords(call).get("title")
		if title is not None and title.type == "string":
			yield Hit.at(title)


@rule(
	"frappe-missing-translate-function-in-report-python",
	"python",
	"P2",
	"translation",
	"Report label not translated",
	"Users see this column or filter label, and it is not wrapped in `_()`.",
	comment_type="style",
	paths=lambda path: (under("report")(path) or named("report.py")(path)) and not under("regional")(path),
)
def untranslated_report_label(source: Source) -> Iterator[Hit]:
	for pair in s.of_type(source.root, "pair"):
		if s.string_value(pair.child_by_field_name("key")) == "label":
			value = pair.child_by_field_name("value")
			if value is not None and value.type == "string":
				yield Hit.at(pair)
	for call in _calls(source, "dict", "_dict", "frappe._dict"):
		label = s.keywords(call).get("label")
		if label is not None and label.type == "string":
			yield Hit.at(label)


# ----------------------------------------------------------------- style


@rule(
	"overusing-args",
	"python",
	"P2",
	"maintainability",
	"Function takes `args`",
	"A single `args` parameter hides what the function needs. Name the parameters.",
	on=False,
	comment_type="style",
)
def overusing_args(source: Source) -> Iterator[Hit]:
	for function in s.of_type(source.root, "function_definition"):
		parameters = function.child_by_field_name("parameters")
		named_parameters = parameters.named_children if parameters is not None else []
		if len(named_parameters) == 1 and s.text(named_parameters[0]) == "args":
			yield Hit.at(named_parameters[0])


@rule(
	"useless-get-doc-dict",
	"python",
	"P2",
	"maintainability",
	"`get_doc(dict(...))`",
	"`frappe.get_doc(dict(k=v))` is the same as `frappe.get_doc(k=v)`.",
	on=False,
	comment_type="style",
)
def get_doc_dict(source: Source) -> Iterator[Hit]:
	for call in _calls(source, "frappe.get_doc"):
		args = s.arguments(call)
		if len(args) == 1 and args[0].type == "call" and s.callee(args[0]) == "dict" and not s.keywords(call):
			yield Hit.at(call)


@rule(
	"frappe-print-function-in-doctypes",
	"python",
	"P2",
	"maintainability",
	"`print` in a DocType",
	"`print` output goes to the worker log, where nobody reads it. Use `frappe.logger()` or `frappe.msgprint`.",
	on=False,
	comment_type="style",
	paths=under("doctype"),
)
def print_in_doctype(source: Source) -> Iterator[Hit]:
	yield from (Hit.at(call) for call in _calls(source, "print"))


@rule(
	"frappe-no-functional-code",
	"python",
	"P2",
	"maintainability",
	"`map` or `filter`",
	"A list comprehension or a generator is easier to read than `map` and `filter`.",
	on=False,
	comment_type="style",
)
def functional_code(source: Source) -> Iterator[Hit]:
	yield from (Hit.at(call) for call in _calls(source, "map", "filter"))


@rule(
	"missing-argument-type-hint",
	"python",
	"P2",
	"maintainability",
	"Argument of an endpoint has no type",
	"`{name}` of `{function}` has no type hint. Frappe checks the types of a whitelisted method's arguments only when they have hints.",
	on=False,
	comment_type="style",
)
def missing_type_hint(source: Source) -> Iterator[Hit]:
	for function, _decorator in _whitelisted(source):
		parameters = function.child_by_field_name("parameters")
		for parameter in parameters.named_children if parameters is not None else []:
			if parameter.type == "default_parameter":
				name = s.text(parameter.child_by_field_name("name"))
			elif parameter.type == "identifier":
				name = s.text(parameter)
			else:
				continue
			if name not in ("self", "cls"):
				yield Hit.at(parameter, name=name, function=s.name_of(function))
