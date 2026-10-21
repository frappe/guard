"""The review prompt: context files and question lists."""

from dataclasses import dataclass, field
from pathlib import PurePosixPath

from guard.review.run import Lister, Reader


def read_file(read: Reader, path: str, limit: int = 400) -> str:
	text = read(path) or ""
	lines = text.splitlines()
	if len(lines) > limit:
		return "\n".join(lines[:limit]) + f"\n… ({len(lines) - limit} more lines)"
	return text


@dataclass
class ContextFile:
	path: str
	content: str
	reason: str  # why it is in the prompt
	limit: int = 400

	def text(self) -> str:
		return f"--- {self.path} ({self.reason}) ---\n{self.content}"


@dataclass
class Question:
	id: str
	question: str
	kind: str = "verify"  # verify | explore | missing

	def as_text(self) -> str:
		return f"- [{self.id}] ({self.kind}) {self.question}"


@dataclass
class ContextPack:
	files: list[ContextFile] = field(default_factory=list)
	questions: list[Question] = field(default_factory=list)
	notes: list[str] = field(default_factory=list)

	def text(self) -> str:
		return "\n\n".join(f.text() for f in self.files) + "\n\n" + questions_text(self.questions)


def questions_text(questions: list[Question]) -> str:
	if not questions:
		return ""
	return "Questions to answer before reporting:\n" + "\n".join(q.as_text() for q in questions)


def context_file(read: Reader, path: str, reason: str, limit: int = 400) -> ContextFile | None:
	content = read_file(read, path, limit)
	if not content.strip():
		return None
	return ContextFile(path, content, reason, limit)


def tree(list_dir: Lister, path: str, prefix: str = "", max_entries: int = 60) -> list[str]:
	"""Sorted directory entries, flattened one level deep."""
	try:
		entries = sorted(list_dir(path))
	except Exception:
		return []
	lines = []
	for name, kind in entries[:max_entries]:
		lines.append(f"{prefix}{name}{'/' if kind == 'tree' else ''}")
	if len(entries) > max_entries:
		lines.append(f"… {len(entries) - max_entries} more")
	return lines


def top_directories(list_dir: Lister, depth: int = 2) -> list[str]:
	"""Directory tree overview of the repository."""
	lines = tree(list_dir, "")
	for name, _ in sorted(e for e in _entries(list_dir, "") if e[1] == "tree")[:12]:
		lines += ["  " + line for line in tree(list_dir, name, f"{name}/", 15)]
	return lines


def _entries(list_dir: Lister, path: str) -> list[tuple[str, str]]:
	try:
		return list_dir(path)
	except Exception:
		return []


def source_paths(diff_path: str) -> list[str]:
	"""Likely module paths for a file path: the stem and each directory as a python or js module."""
	pure = PurePosixPath(diff_path)
	candidates = [str(pure.with_suffix(""))]
	for i in range(1, len(pure.parts)):
		candidates.append("/".join(pure.parts[:i]))
	return candidates
