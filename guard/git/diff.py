"""Parse unified diffs into files, hunks and lines with line numbers."""

import re
from dataclasses import dataclass, field

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")


@dataclass
class DiffLine:
	kind: str  # " ", "+", "-"
	text: str
	old_no: int | None
	new_no: int | None


@dataclass
class Hunk:
	id: str
	old_start: int
	old_count: int
	new_start: int
	new_count: int
	header: str
	lines: list[DiffLine] = field(default_factory=list)

	@property
	def new_end(self) -> int:
		return self.new_start + max(self.new_count, 1) - 1

	@property
	def changed_new_lines(self) -> list[int]:
		return [line.new_no for line in self.lines if line.kind == "+" and line.new_no]

	def text(self) -> str:
		body = "\n".join(f"{line.kind}{line.text}" for line in self.lines)
		return f"@@ -{self.old_start},{self.old_count} +{self.new_start},{self.new_count} @@ {self.header}\n{body}"


@dataclass
class FileDiff:
	path: str
	old_path: str
	status: str  # added, modified, deleted, renamed
	is_binary: bool = False
	hunks: list[Hunk] = field(default_factory=list)

	@property
	def additions(self) -> int:
		return sum(1 for h in self.hunks for line in h.lines if line.kind == "+")

	@property
	def deletions(self) -> int:
		return sum(1 for h in self.hunks for line in h.lines if line.kind == "-")

	def commentable_lines(self) -> set[int]:
		"""Head lines a provider accepts an inline comment on: added and context lines."""
		return {line.new_no for h in self.hunks for line in h.lines if line.kind != "-" and line.new_no}

	def added_lines(self) -> set[int]:
		return {line.new_no for h in self.hunks for line in h.lines if line.kind == "+" and line.new_no}

	def touches(self, start: int, end: int, margin: int = 0) -> bool:
		"""True when a change in this diff falls inside the head range."""
		low, high = start - margin, end + margin
		for hunk in self.hunks:
			for line in hunk.lines:
				if line.kind == "+" and line.new_no and low <= line.new_no <= high:
					return True
				if line.kind == "-" and line.old_no and low <= line.old_no <= high:
					return True
		return False

	def touches_old(self, start: int, end: int, margin: int = 0) -> bool:
		"""True when this diff removes or replaces lines of the old side inside the range."""
		low, high = start - margin, end + margin
		for hunk in self.hunks:
			if hunk.old_start > high or hunk.old_start + max(hunk.old_count, 1) - 1 < low:
				continue
			for line in hunk.lines:
				if line.kind == "-" and line.old_no and low <= line.old_no <= high:
					return True
			# a pure insertion inside the range also changes it
			if any(line.kind == "+" for line in hunk.lines) and low <= hunk.old_start <= high:
				return True
		return False

	def annotated(self, head: list[str] | None = None, pad: int = 0) -> str:
		"""The diff with head line numbers, for the model.

		With the `head` lines of the file, each hunk also shows up to `pad` more lines of context on
		each side. A gap between two hunks that is shorter than that is shown whole, and once.
		"""
		out = [f"### {self.path} ({self.status})"]
		shown = 0  # the last head line written
		for hunk in self.hunks:
			first = _first_new(hunk)
			if head and pad:
				if shown and first - shown - 1 > 2 * pad:
					out += _context(head, shown + 1, shown + pad)
					shown = first - pad - 1
				out.append(f"@@ {hunk.id} {hunk.header}".rstrip())
				out += _context(head, max(shown + 1, first - pad), first - 1)
			else:
				out.append(f"@@ {hunk.id} {hunk.header}".rstrip())
			for line in hunk.lines:
				number = line.new_no if line.kind != "-" else line.old_no
				label = f"{number:>5}" if line.kind != "-" else f"{'':>5}"
				out.append(f"{label} {line.kind} {line.text}")
			shown = max(shown, _last_new(hunk))
		if head and pad and self.hunks:
			out += _context(head, shown + 1, shown + pad)
		return "\n".join(out)

	def nearest_commentable(self, line: int) -> int | None:
		lines = sorted(self.commentable_lines())
		if not lines:
			return None
		return min(lines, key=lambda candidate: abs(candidate - line))


def _first_new(hunk: Hunk) -> int:
	"""The first head line of the hunk. A hunk that only deletes starts after `new_start`."""
	return next((line.new_no for line in hunk.lines if line.new_no), hunk.new_start + 1)


def _last_new(hunk: Hunk) -> int:
	return max((line.new_no for line in hunk.lines if line.new_no), default=hunk.new_start)


def _context(head: list[str], start: int, end: int) -> list[str]:
	return [f"{number:>5}   {head[number - 1]}" for number in range(max(start, 1), min(end, len(head)) + 1)]


def parse_diff(text: str) -> list[FileDiff]:
	files: list[FileDiff] = []
	current: FileDiff | None = None
	hunk: Hunk | None = None
	old_no = new_no = 0
	for raw in text.splitlines():
		if raw.startswith("diff --git "):
			current = _start_file(raw)
			files.append(current)
			hunk = None
			continue
		if current is None:
			continue
		# Header lines come only before the first hunk of a file.
		if hunk is None and _file_header(current, raw):
			continue
		match = HUNK_HEADER.match(raw)
		if match:
			hunk = Hunk(
				id=f"h{sum(len(f.hunks) for f in files) + 1}",
				old_start=int(match.group(1)),
				old_count=int(match.group(2) or 1),
				new_start=int(match.group(3)),
				new_count=int(match.group(4) or 1),
				header=match.group(5).strip(),
			)
			current.hunks.append(hunk)
			old_no, new_no = hunk.old_start, hunk.new_start
			continue
		if hunk is None or not raw or raw.startswith("\\"):
			continue
		kind, body = raw[0], raw[1:]
		if kind == "+":
			hunk.lines.append(DiffLine("+", body, None, new_no))
			new_no += 1
		elif kind == "-":
			hunk.lines.append(DiffLine("-", body, old_no, None))
			old_no += 1
		elif kind == " ":
			hunk.lines.append(DiffLine(" ", body, old_no, new_no))
			old_no += 1
			new_no += 1
	return files


def _start_file(line: str) -> FileDiff:
	match = re.match(r"^diff --git a/(.+?) b/(.+)$", line)
	old, new = (match.group(1), match.group(2)) if match else ("", "")
	return FileDiff(path=new, old_path=old, status="modified")


def _file_header(current: FileDiff, raw: str) -> bool:
	if raw.startswith("new file mode"):
		current.status = "added"
	elif raw.startswith("deleted file mode"):
		current.status = "deleted"
	elif raw.startswith("rename from "):
		current.old_path, current.status = raw[len("rename from ") :], "renamed"
	elif raw.startswith("rename to "):
		current.path = raw[len("rename to ") :]
	elif raw.startswith("Binary files") or raw.startswith("GIT binary patch"):
		current.is_binary = True
	elif raw.startswith("--- ") or raw.startswith("+++ "):
		target = raw[4:]
		if raw.startswith("+++ ") and target != "/dev/null":
			current.path = target[2:] if target.startswith("b/") else target
	elif not raw.startswith(
		("index ", "similarity index", "dissimilarity index", "old mode", "new mode", "copy ")
	):
		return False
	return True
