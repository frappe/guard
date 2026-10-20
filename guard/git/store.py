"""One bare mirror for each repository, driven by the git CLI."""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

import frappe

from guard.utils.redact import redact

TIMEOUT = 600
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


class GitError(Exception):
	pass


@dataclass
class TreeEntry:
	path: str
	blob: str
	size: int


@dataclass
class Commit:
	sha: str
	author_email: str
	subject: str
	body: str
	date: str


class GitStore:
	def __init__(self, name: str, clone_url: str, auth_header: str | None = None):
		self.name = name
		self.clone_url = clone_url
		self.auth_header = auth_header
		self.path = Path(frappe.get_site_path("private", "guard", "git", f"{name}.git"))

	# ------------------------------------------------------------- remote

	def ensure(self) -> None:
		if (self.path / "HEAD").exists():
			self._git("remote", "set-url", "origin", self.clone_url)
			return
		self.path.mkdir(parents=True, exist_ok=True)
		self._git("init", "--bare", "--quiet")
		self._git("remote", "add", "origin", self.clone_url)

	def fetch(self, *refspecs: str) -> None:
		"""Fetch branches, or the given refspecs."""
		self.ensure()
		specs = refspecs or ("+refs/heads/*:refs/heads/*",)
		self._git("fetch", "--quiet", "--prune", "--no-tags", "origin", *specs, auth=True)

	def fetch_pull(self, pull_ref: str, number: int) -> str:
		"""Fetch a pull request head into `refs/guard/pr/<n>` and return its sha."""
		ref = f"refs/guard/pr/{number}"
		self.fetch(f"+{pull_ref}:{ref}")
		return self.rev_parse(ref)

	def fetch_sha(self, sha: str, url: str | None = None) -> None:
		"""Fetch one commit, from a fork when `url` is given."""
		self.ensure()
		if self.has_commit(sha):
			return
		self._git("fetch", "--quiet", "--no-tags", url or "origin", sha, auth=True)

	# --------------------------------------------------------------- reads

	def has_commit(self, sha: str) -> bool:
		try:
			self._git("cat-file", "-e", f"{sha}^{{commit}}")
		except GitError:
			return False
		return True

	def rev_parse(self, ref: str) -> str:
		return self._git("rev-parse", "--verify", f"{ref}^{{commit}}").strip()

	def merge_base(self, a: str, b: str) -> str:
		return self._git("merge-base", a, b).strip()

	def is_ancestor(self, ancestor: str, descendant: str) -> bool:
		try:
			self._git("merge-base", "--is-ancestor", ancestor, descendant)
		except GitError:
			return False
		return True

	def ls_tree(self, sha: str) -> list[TreeEntry]:
		out = self._git("ls-tree", "-r", "-l", "-z", sha)
		entries = []
		for record in out.split("\0"):
			if not record:
				continue
			meta, _, path = record.partition("\t")
			parts = meta.split()
			if len(parts) == 4 and parts[1] == "blob":
				size = int(parts[3]) if parts[3].isdigit() else 0
				entries.append(TreeEntry(path=path, blob=parts[2], size=size))
		return entries

	def list_dir(self, sha: str, path: str = "") -> list[tuple[str, str]]:
		"""(name, type) pairs in one directory. Type is `blob` or `tree`."""
		target = f"{sha}:{path.strip('/')}" if path.strip("/") else sha
		out = self._git("ls-tree", target)
		rows = []
		for line in out.splitlines():
			meta, _, name = line.partition("\t")
			rows.append((name, meta.split()[1]))
		return rows

	def show(self, sha: str, path: str) -> str | None:
		try:
			return self._git("show", f"{sha}:{path}")
		except GitError:
			return None

	def read_blobs(self, blobs: list[str]) -> dict[str, bytes]:
		"""Read many blobs with one `cat-file --batch` call."""
		if not blobs:
			return {}
		process = subprocess.run(
			["git", "-C", str(self.path), "cat-file", "--batch"],
			input=("\n".join(blobs) + "\n").encode(),
			capture_output=True,
			timeout=TIMEOUT,
			env=self._env(),
			check=False,
		)
		return _parse_batch(process.stdout)

	def diff(self, base: str, head: str, paths: list[str] | None = None, context: int = 3) -> str:
		args = ["diff", "--no-color", "--no-ext-diff", "--find-renames", f"-U{context}", base, head]
		if paths:
			args += ["--", *paths]
		return self._git(*args)

	def name_status(self, base: str, head: str) -> list[tuple[str, str, str | None]]:
		"""(status letter, path, old path for renames) for each changed file."""
		out = self._git("diff", "--name-status", "--find-renames", "-z", base or EMPTY_TREE, head)
		parts = [p for p in out.split("\0") if p]
		rows, i = [], 0
		while i < len(parts):
			status = parts[i][0]
			if status in ("R", "C"):
				rows.append((status, parts[i + 2], parts[i + 1]))
				i += 3
			else:
				rows.append((status, parts[i + 1], None))
				i += 2
		return rows

	def grep(
		self, sha: str, pattern: str, path_glob: str | None = None, limit: int = 100
	) -> list[tuple[str, int, str]]:
		args = ["grep", "-n", "-I", "-E", "--max-count=20", "-e", pattern, sha]
		if path_glob:
			args += ["--", path_glob]
		try:
			out = self._git(*args)
		except GitError:
			return []
		hits = []
		for line in out.splitlines()[:limit]:
			_, _, rest = line.partition(":")
			path, _, rest = rest.partition(":")
			number, _, text = rest.partition(":")
			if number.isdigit():
				hits.append((path, int(number), text[:300]))
		return hits

	def log(
		self, ref: str, since_days: int | None = None, grep: list[str] | None = None, limit: int = 200
	) -> list[Commit]:
		args = ["log", ref, f"--max-count={limit}", "--format=%H%x1f%ae%x1f%s%x1f%b%x1f%cI%x1e"]
		if since_days:
			args.append(f"--since={since_days}.days")
		for pattern in grep or []:
			args += ["--grep", pattern]
		if grep:
			args += ["-i", "--extended-regexp"]
		out = self._git(*args)
		commits = []
		for record in out.split("\x1e"):
			fields = record.strip("\n").split("\x1f")
			if len(fields) == 5:
				commits.append(Commit(*fields))
		return commits

	def commits_between(self, base: str, head: str) -> list[Commit]:
		return self.log(f"{base}..{head}", limit=250)

	def size_kb(self) -> int:
		total = sum(f.stat().st_size for f in self.path.rglob("*") if f.is_file())
		return total // 1024

	# ------------------------------------------------------------ internal

	def _git(self, *args: str, auth: bool = False) -> str:
		command = ["git", "-C", str(self.path)]
		if auth and self.auth_header:
			command += ["-c", f"http.extraHeader={self.auth_header}"]
		command += list(args)
		try:
			result = subprocess.run(
				command, capture_output=True, timeout=TIMEOUT, env=self._env(), check=False
			)
		except subprocess.TimeoutExpired as error:
			raise GitError(f"git {args[0]} timed out") from error
		if result.returncode != 0:
			message = result.stderr.decode("utf-8", "replace").strip()
			raise GitError(redact(f"git {args[0]} failed: {message}"[:2000]))
		return result.stdout.decode("utf-8", "replace")

	def _env(self) -> dict[str, str]:
		env = dict(os.environ)
		env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true", "LC_ALL": "C"})
		return env


def _parse_batch(data: bytes) -> dict[str, bytes]:
	blobs: dict[str, bytes] = {}
	position = 0
	while position < len(data):
		newline = data.index(b"\n", position)
		header = data[position:newline].decode().split()
		position = newline + 1
		if len(header) < 3 or header[1] == "missing":
			continue
		size = int(header[2])
		blobs[header[0]] = data[position : position + size]
		position += size + 1
	return blobs
