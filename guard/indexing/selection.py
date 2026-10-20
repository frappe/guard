"""Which files of a commit the indexer reads."""

import pathspec

from guard.git.store import GitStore, TreeEntry

MAX_FILE_BYTES = 1024 * 1024
MAX_FILES = 200_000
SKIP_DIRS = (
	"node_modules/",
	"vendor/",
	"dist/",
	"build/",
	".git/",
	"__pycache__/",
	".venv/",
	"venv/",
	"coverage/",
)
SKIP_SUFFIXES = (
	".min.js",
	".min.css",
	".map",
	".lock",
	"package-lock.json",
	"yarn.lock",
	"pnpm-lock.yaml",
	"poetry.lock",
	"uv.lock",
	"Cargo.lock",
	"go.sum",
)
BINARY_SUFFIXES = (
	".png",
	".jpg",
	".jpeg",
	".gif",
	".webp",
	".ico",
	".pdf",
	".zip",
	".gz",
	".tar",
	".woff",
	".woff2",
	".ttf",
	".eot",
	".otf",
	".mp4",
	".mp3",
	".wav",
	".so",
	".dll",
	".exe",
	".bin",
	".jar",
	".pyc",
	".class",
	".sqlite",
	".db",
	".psd",
	".svgz",
)


class FileSelector:
	def __init__(self, store: GitStore, sha: str):
		self.store = store
		self.sha = sha
		self.generated = self._generated_spec()

	def select(self, entries: list[TreeEntry]) -> dict[str, TreeEntry]:
		chosen = {}
		for entry in entries:
			if self.wanted(entry):
				chosen[entry.path] = entry
				if len(chosen) >= MAX_FILES:
					break
		return chosen

	def wanted(self, entry: TreeEntry) -> bool:
		path = entry.path
		lower = path.lower()
		if entry.size > MAX_FILE_BYTES or entry.size == 0:
			return False
		if any(f"/{d}" in f"/{path}" for d in SKIP_DIRS):
			return False
		if lower.endswith(SKIP_SUFFIXES) or lower.endswith(BINARY_SUFFIXES):
			return False
		return not (self.generated and self.generated.match_file(path))

	def _generated_spec(self) -> pathspec.PathSpec | None:
		text = self.store.show(self.sha, ".gitattributes") or ""
		patterns = [
			line.split()[0]
			for line in text.splitlines()
			if line.strip()
			and not line.startswith("#")
			and ("linguist-generated" in line or "linguist-vendored" in line)
		]
		return pathspec.PathSpec.from_lines("gitwildmatch", patterns) if patterns else None


def is_text(data: bytes) -> bool:
	return b"\0" not in data[:8192]
