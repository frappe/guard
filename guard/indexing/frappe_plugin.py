"""Frappe-aware graph: doctypes, links, hooks, whitelisted methods, frappe.call."""

import json
import posixpath
import re

from guard.indexing.parser import ParsedEntity, ParsedFile, RawEdge

LINK_TYPES = {"Link", "Table", "Table MultiSelect"}
CALL_HINT = re.compile(
	r"frappe\.(?:x)?call|\bmethod\s*:|\burl\s*:|\bcall\(|frm\.call|enqueue\(|createResource"
)
HOOK_DOCTYPE = re.compile(r"""^\s*['"]([A-Z][\w ]+)['"]\s*:\s*\{""")


def is_frappe_app(paths: set[str]) -> bool:
	for path in paths:
		if path.endswith("hooks.py") and posixpath.join(posixpath.dirname(path), "modules.txt") in paths:
			return True
	return False


def augment(parsed: ParsedFile, source: str) -> None:
	"""Add Frappe entities and edges to one parsed file."""
	if _is_doctype_json(parsed.path):
		_doctype(parsed, source)
		return
	if parsed.language == "python":
		for entity in parsed.entities:
			if any("frappe.whitelist" in d for d in entity.decorators):
				entity.is_entry_point = True
				entity.signature = "\n".join([*entity.decorators, entity.signature])
	if parsed.language in ("javascript", "typescript", "vue"):
		lines = source.splitlines()
		for edge in parsed.edges:
			if (
				edge.kind == "references"
				and 0 < edge.line <= len(lines)
				and CALL_HINT.search(lines[edge.line - 1])
			):
				edge.kind = "calls"
	if parsed.path.endswith("hooks.py"):
		for number, line in enumerate(source.splitlines(), 1):
			if match := HOOK_DOCTYPE.match(line):
				name = match.group(1)
				parsed.edges.append(RawEdge(parsed.path, "references", f"doctype:{name}", name, number))


def controller_edges(paths: set[str]) -> list[tuple[str, str]]:
	"""(controller path, doctype JSON path) pairs, for `references` edges."""
	pairs = []
	for path in paths:
		if not _is_doctype_json(path):
			continue
		stem = path[: -len(".json")]
		for extension in (".py", ".js", "_list.js"):
			if stem + extension in paths:
				pairs.append((stem + extension, path))
	return pairs


def _is_doctype_json(path: str) -> bool:
	parts = path.split("/")
	return len(parts) >= 3 and parts[-3] == "doctype" and parts[-1] == f"{parts[-2]}.json"


def _doctype(parsed: ParsedFile, source: str) -> None:
	try:
		data = json.loads(source)
	except json.JSONDecodeError:
		return
	if not isinstance(data, dict) or data.get("doctype") != "DocType" or not data.get("name"):
		return
	name = data["name"]
	file = parsed.file_entity
	flags = [flag for flag in ("istable", "issingle", "is_submittable", "is_tree") if data.get(flag)]
	doctype = ParsedEntity(
		key=f"doctype:{name}",
		kind="doctype",
		name=name,
		qualified_name=f"doctype:{name}",
		parent_key=file.key,
		start_line=1,
		end_line=parsed.line_count,
		signature=f"DocType {name} ({data.get('module', '')}) {' '.join(flags)}".strip(),
		docstring=(data.get("description") or "")[:500],
		is_exported=True,
	)
	parsed.entities.append(doctype)
	parsed.edges.append(RawEdge(file.key, "contains", name, name, 1, doctype.key))
	lines = source.splitlines()
	for field in data.get("fields") or []:
		fieldname = field.get("fieldname")
		if not fieldname:
			continue
		line = _line_of(lines, f'"fieldname": "{fieldname}"')
		options = field.get("options") or ""
		entity = ParsedEntity(
			key=f"doctype:{name}.{fieldname}",
			kind="field",
			name=fieldname,
			qualified_name=f"doctype:{name}.{fieldname}",
			parent_key=doctype.key,
			start_line=line,
			end_line=line,
			signature=f"{field.get('fieldtype')} {field.get('label') or ''} {options}".strip()[:300],
		)
		parsed.entities.append(entity)
		parsed.edges.append(RawEdge(doctype.key, "contains", fieldname, fieldname, line, entity.key))
		if field.get("fieldtype") in LINK_TYPES and options:
			parsed.edges.append(RawEdge(entity.key, "references", f"doctype:{options}", options, line))


def _line_of(lines: list[str], needle: str) -> int:
	for number, line in enumerate(lines, 1):
		if needle in line:
			return number
	return 1
