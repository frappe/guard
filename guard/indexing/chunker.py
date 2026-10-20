"""Cut source files into chunks for search."""

from dataclasses import dataclass

from guard.indexing.parser import ParsedFile

MAX_LINES = 150
WINDOW = 100
OVERLAP = 15


@dataclass
class Chunk:
	path: str
	start_line: int
	end_line: int
	entity_key: str | None
	content: str


def chunk_file(parsed: ParsedFile, source: str) -> list[Chunk]:
	lines = source.splitlines()
	if not lines:
		return []
	chunks: list[Chunk] = []
	covered = [False] * (len(lines) + 2)
	for entity in parsed.entities[1:]:
		if entity.kind in ("function", "method"):
			chunks += _body_chunks(
				parsed.path, lines, entity.start_line, entity.end_line, entity.key, entity.qualified_name
			)
			_cover(covered, entity.start_line, entity.end_line)
		elif entity.kind in ("class", "interface", "type", "doctype"):
			end = _header_end(parsed, entity.key, entity.start_line, entity.end_line)
			chunks.append(
				_chunk(parsed.path, lines, entity.start_line, end, entity.key, entity.qualified_name)
			)
			_cover(covered, entity.start_line, end)
	for start, end in _uncovered_ranges(covered, len(lines)):
		chunks += _body_chunks(parsed.path, lines, start, end, None, parsed.module)
	return [chunk for chunk in chunks if chunk.content.strip() and not _is_markup_blob(chunk.content)]


def _body_chunks(path, lines, start, end, key, label) -> list[Chunk]:
	if end - start + 1 <= MAX_LINES:
		return [_chunk(path, lines, start, end, key, label)]
	out = []
	position = start
	while position <= end:
		stop = min(position + WINDOW - 1, end)
		out.append(_chunk(path, lines, position, stop, key, label))
		if stop == end:
			break
		position = stop - OVERLAP + 1
	return out


def _header_end(parsed: ParsedFile, key: str, start: int, end: int) -> int:
	"""A class header runs until its first member."""
	members = [e.start_line for e in parsed.entities if e.parent_key == key and e.start_line > start]
	header_end = (min(members) - 1) if members else end
	return min(header_end, start + MAX_LINES - 1)


def _chunk(path, lines, start, end, key, label) -> Chunk:
	body = "\n".join(lines[start - 1 : end])
	return Chunk(path, start, end, key, f"{path}:{start}-{end} {label}\n{body}")


def _cover(covered: list[bool], start: int, end: int) -> None:
	for line in range(max(start, 1), min(end, len(covered) - 2) + 1):
		covered[line] = True


def _uncovered_ranges(covered: list[bool], total: int) -> list[tuple[int, int]]:
	ranges, start = [], None
	for line in range(1, total + 1):
		if not covered[line] and start is None:
			start = line
		elif covered[line] and start is not None:
			ranges.append((start, line - 1))
			start = None
	if start is not None:
		ranges.append((start, total))
	return ranges


def _is_markup_blob(content: str) -> bool:
	"""SVG paths and minified lines make poor search results."""
	lines = content.splitlines()[1:]
	if not lines:
		return True
	long_lines = sum(1 for line in lines if len(line) > 400)
	return content.count("<path") > 5 or long_lines > len(lines) / 2
