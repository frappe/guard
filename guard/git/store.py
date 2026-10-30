"""One bare mirror for each repository, driven by the git CLI."""

import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import frappe

from guard.utils.locks import file_lock
from guard.utils.redact import redact

TIMEOUT = 600
EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
# A fetch starts git's automatic gc. On a big mirror that is a detached repack on every core.
NO_GC = ("-c", "gc.auto=0", "-c", "maintenance.auto=false")


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
		# New objects go here instead of into the mirror. See `scratch`.
		self.objects: Path | None = None

	def scratch(self, directory: Path) -> "GitStore":
		"""This mirror, with new objects written to `directory`. The mirror stays unchanged, and
		deleting `directory` deletes them. A repack would keep objects that no ref reaches forever."""
		store = GitStore(self.name, self.clone_url, self.auth_header)
		store.objects = directory
		directory.mkdir(parents=True, exist_ok=True)
		return store

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
		specs = refspecs or ("+refs/heads/*:refs/heads/*",)
		with self._lock():
			self.ensure()
			self._git("fetch", "--quiet", "--prune", "--no-tags", "origin", *specs, auth=True)

	def fetch_branches(self, *branches: str) -> None:
		"""Fetch only these branches, or every branch when none is given.

		A big repository has hundreds of branches, and most of them change every hour.
		A review needs only its base branch.
		"""
		names = [b for b in dict.fromkeys(branches) if b]
		self.fetch(*(f"+refs/heads/{name}:refs/heads/{name}" for name in names))

	def fetch_pull(self, pull_ref: str, number: int) -> str:
		"""Fetch a pull request head into `refs/guard/pr/<n>` and return its sha."""
		ref = f"refs/guard/pr/{number}"
		self.fetch(f"+{pull_ref}:{ref}")
		return self.rev_parse(ref)

	def fetch_branch(self, branch: str, number: int, url: str | None = None) -> str:
		"""Fetch a head branch, from a fork when `url` is given, into `refs/guard/pr/<n>`."""
		ref = f"refs/guard/pr/{number}"
		with self._lock():
			self.ensure()
			self._git(
				"fetch", "--quiet", "--no-tags", url or "origin", f"+refs/heads/{branch}:{ref}", auth=True
			)
		return self.rev_parse(ref)

	def fetch_sha(self, sha: str, url: str | None = None) -> None:
		"""Fetch one commit, from a fork when `url` is given."""
		with self._lock():
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

	def conflicts(self, base: str, head: str) -> bool:
		"""True when a merge of `head` into `base` has conflicts. An error counts as no conflict."""
		try:
			process = subprocess.run(
				["git", "-C", str(self.path), *NO_GC, "merge-tree", "--write-tree", "--quiet", base, head],
				capture_output=True,
				timeout=TIMEOUT,
				env=self._env(),
				check=False,
			)
		except subprocess.TimeoutExpired:
			return False
		# Exit code 1 with no message is a conflict. A bad ref also exits with 1, but prints why.
		return process.returncode == 1 and not process.stderr.strip()

	def refs(self, prefix: str) -> list[str]:
		return self._git("for-each-ref", "--format=%(refname)", prefix).split()

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
			["git", "-C", str(self.path), *NO_GC, "cat-file", "--batch"],
			input=("\n".join(blobs) + "\n").encode(),
			capture_output=True,
			timeout=TIMEOUT,
			env=self._env(),
			check=False,
		)
		return _parse_batch(process.stdout)

	def apply(self, base: str, patch: str) -> str:
		"""Write the tree of `base` with `patch` applied, and return its sha. No ref points at it.

		A temporary index holds the result, so no ref changes. Use it on a `scratch` store, so the
		objects do not go into the mirror. Raise GitError when the patch does not apply to `base`.
		"""
		with tempfile.TemporaryDirectory(prefix="guard-apply-") as directory:
			env = {"GIT_INDEX_FILE": str(Path(directory) / "index")}
			self._git("read-tree", base, env=env)
			self._git("apply", "--cached", "--whitespace=nowarn", stdin=patch, env=env)
			return self._git("write-tree", env=env).strip()

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
		"""Loose and packed objects, in KiB."""
		counts = dict(line.split(": ", 1) for line in self._git("count-objects", "-v").splitlines())
		return int(counts.get("size", 0)) + int(counts.get("size-pack", 0))

	# --------------------------------------------------------- maintenance

	def maintain(self) -> None:
		"""Roll loose objects and small packs into bigger ones, on one thread, and pack the refs."""
		with self._lock():
			self._git("repack", "-d", "-l", "--geometric=2", "--quiet", config=("pack.threads=1",))
			self._git("pack-refs", "--all")

	def delete_refs(self, refs: list[str]) -> None:
		if not refs:
			return
		with self._lock():
			self._git("update-ref", "--stdin", stdin="".join(f"delete {ref}\n" for ref in refs))

	# ------------------------------------------------------------ internal

	@contextmanager
	def _lock(self) -> Iterator[None]:
		"""Held while git writes refs or config: two fetches into one repository fail to lock a ref.
		The kernel drops the lock when a process dies."""
		with file_lock(self.path / "guard.lock", wait=TIMEOUT) as acquired:
			if not acquired:
				raise GitError(f"{self.name} is locked by another git command")
			yield

	def _git(
		self,
		*args: str,
		auth: bool = False,
		stdin: str | None = None,
		config: tuple[str, ...] = (),
		env: dict[str, str] | None = None,
	) -> str:
		command = ["git", "-C", str(self.path), *NO_GC]
		if auth and self.auth_header:
			command += ["-c", f"http.extraHeader={self.auth_header}"]
		for option in config:
			command += ["-c", option]
		command += list(args)
		try:
			result = subprocess.run(
				command,
				input=stdin.encode() if stdin is not None else None,
				capture_output=True,
				timeout=TIMEOUT,
				env={**self._env(), **(env or {})},
				check=False,
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
		if self.objects is not None:
			# Absolute: git reads these after `-C` changes the directory, and site paths are relative.
			env["GIT_OBJECT_DIRECTORY"] = str(self.objects.resolve())
			env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = str((self.path / "objects").resolve())
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
