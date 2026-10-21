"""Resolve the review config of a file and of a pull request.

Layers, lowest priority first: dashboard (organization or team), dashboard custom
context, repository overrides, then TOML config from the repository root down to
the file. Settings: the closest value wins. Rules, files, notes and instructions add up.
"""

import copy
import posixpath
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from guard.config import dashboard
from guard.config.globs import match_path
from guard.config.schema import (
	ADDITIVE,
	BOOLEANS,
	DEFAULTS,
	EVENTS,
	RISKS,
	SECTIONS,
	TIER_RANK,
	FileRef,
	Layer,
	Note,
	Rule,
	events_for_level,
	files_from,
	normalize,
	review_level,
)
from guard.git.store import GitStore

GUARD_DIR = ".guard"
SINGLE_FILE = "guard.toml"


@dataclass
class FileConfig:
	path: str
	settings: dict[str, Any]
	provenance: dict[str, str]
	rules: list[Rule] = field(default_factory=list)
	files: list[FileRef] = field(default_factory=list)
	notes: list[Note] = field(default_factory=list)
	instructions: list[tuple[str, str]] = field(default_factory=list)
	rules_md: list[tuple[str, str]] = field(default_factory=list)
	repos: list[str] = field(default_factory=list)
	efforts: list[str] = field(default_factory=list)
	ignored: bool = False


@dataclass
class PullRequestConfig:
	settings: dict[str, Any]
	files: dict[str, FileConfig]
	efforts: list[str]
	repos: list[str]
	errors: list[str]

	def reviewed_paths(self) -> list[str]:
		return [path for path, config in self.files.items() if not config.ignored]

	def section(self, name: str) -> dict[str, bool]:
		return self.settings["sections"][name]


class ConfigResolver:
	def __init__(
		self,
		repository: str | None,
		reader: Callable[[str], str | None],
		paths: list[str],
		team: str | None = None,
		base_layers: list[Layer] | None = None,
	):
		self.repository = repository
		self.reader = reader
		self.errors: list[str] = []
		self.base_layers = base_layers if base_layers is not None else dashboard.layers(repository, team)
		self.layers = self._discover(paths)

	@classmethod
	def from_store(
		cls, repository: str | None, store: GitStore, sha: str, team: str | None = None
	) -> "ConfigResolver":
		paths = [entry.path for entry in store.ls_tree(sha) if is_config_path(entry.path)]
		return cls(repository, lambda path: store.show(sha, path), paths, team)

	@classmethod
	def from_files(
		cls, repository: str | None, files: dict[str, str], team: str | None = None
	) -> "ConfigResolver":
		return cls(repository, files.get, [p for p in files if is_config_path(p)], team)

	# ------------------------------------------------------------ resolve

	def resolve(self, path: str) -> FileConfig:
		settings = copy.deepcopy(DEFAULTS)
		provenance = dict.fromkeys(settings, "default")
		config = FileConfig(path=path, settings=settings, provenance=provenance)
		ignore: list[str] = []
		for layer in self._layers_for(path):
			for key, value in layer.settings.items():
				if key == "sections":
					for name, values in value.items():
						settings["sections"][name] = {**settings["sections"].get(name, {}), **values}
				elif key in ADDITIVE:
					settings[key] = list(dict.fromkeys([*settings[key], *value]))
				else:
					settings[key] = value
				provenance[key] = layer.source
			if layer.settings.get("effort"):
				config.efforts.append(layer.settings["effort"])
			config.rules = [
				r
				for r in [*config.rules, *(r for r in layer.rules if r.enabled)]
				if r.id not in layer.disabled_rules
			]
			config.files += layer.files
			config.notes += layer.notes
			config.repos += [r for r in layer.repos if r not in config.repos]
			ignore += layer.ignore_patterns
			if layer.instructions:
				config.instructions.append((layer.source, layer.instructions))
			if layer.rules_md:
				config.rules_md.append((layer.source, layer.rules_md))
		config.rules = [r for r in config.rules if not r.scope or match_path(path, r.scope)]
		config.files = [f for f in config.files if not f.scope or match_path(path, f.scope)]
		config.notes = [n for n in config.notes if not n.scope or match_path(path, n.scope)]
		config.ignored = bool(path) and match_path(path, ignore)
		return config

	def resolve_pr(self, paths: list[str]) -> PullRequestConfig:
		files = {path: self.resolve(path) for path in paths}
		root = self.resolve("")
		active = [c for c in files.values() if not c.ignored] or [root]
		settings = copy.deepcopy(root.settings)
		settings["strictness"] = max(c.settings["strictness"] for c in active)
		settings["comment_types"] = sorted({t for c in active for t in c.settings["comment_types"]})
		settings["auto_review"] = events_for_level(
			max(review_level(c.settings["auto_review"]) for c in active)
		)
		for name in BOOLEANS:
			settings[name] = any(c.settings[name] for c in active)
		for name in SECTIONS:
			shown = [
				c.settings["sections"][name] for c in active if c.settings["sections"][name].get("included")
			]
			settings["sections"][name] = dict(shown[0]) if shown else dict(root.settings["sections"][name])
		efforts = sorted({e for c in [root, *active] for e in c.efforts}, key=lambda e: TIER_RANK.get(e, 0))
		repos = list(dict.fromkeys(r for c in [root, *active] for r in c.repos))
		return PullRequestConfig(
			settings=settings, files=files, efforts=efforts, repos=repos, errors=self.errors
		)

	def auto_approve_policy(self, paths: list[str]) -> dict[str, Any]:
		"""Strictest merge of the dashboard policy and every config the paths touch."""
		policies = [layer.auto_approve for layer in self.base_layers if layer.auto_approve is not None]
		seen: set[str] = set()
		for path in paths or [""]:
			for layer in self._file_layers_for(path):
				if layer.auto_approve is not None and layer.source not in seen:
					seen.add(layer.source)
					policies.append(layer.auto_approve)
		return merge_auto_approve(policies)

	def explain(self, path: str) -> dict[str, Any]:
		config = self.resolve(path)
		return {
			"path": path,
			"settings": {
				key: {"value": value, "source": config.provenance.get(key, "default")}
				for key, value in config.settings.items()
			},
			"rules": [
				{"id": r.id, "rule": r.rule, "severity": r.severity, "source": r.source} for r in config.rules
			],
			"files": [
				{"path": f.path, "description": f.description, "source": f.source} for f in config.files
			],
			"notes": [{"content": n.content, "source": n.source} for n in config.notes],
			"instructions": [{"source": s, "text": t} for s, t in config.instructions],
			"rules_md": [{"source": s, "text": t} for s, t in config.rules_md],
			"ignored": config.ignored,
			"errors": self.errors,
		}

	# ------------------------------------------------------------- layers

	def _layers_for(self, path: str) -> list[Layer]:
		return [*self.base_layers, *self._file_layers_for(path)]

	def _file_layers_for(self, path: str) -> list[Layer]:
		parts = [p for p in (posixpath.dirname(path) if path else "").split("/") if p]
		chain = ["", *("/".join(parts[: i + 1]) for i in range(len(parts)))]
		return [self.layers[d] for d in chain if d in self.layers]

	def _discover(self, paths: list[str]) -> dict[str, Layer]:
		directories: dict[str, str] = {}
		for path in paths:
			if f"/{GUARD_DIR}/" in f"/{path}":
				directories[path.split(f"{GUARD_DIR}/")[0].rstrip("/")] = "folder"
			elif posixpath.basename(path) == SINGLE_FILE:
				directories.setdefault(posixpath.dirname(path), "single")
		layers = {}
		for directory, kind in directories.items():
			layer = (
				self._folder_layer(directory)
				if kind == "folder"
				else self._file_layer(_join(directory, SINGLE_FILE), directory)
			)
			self.errors += layer.errors
			layers[directory] = layer
		return layers

	def _folder_layer(self, directory: str) -> Layer:
		base = _join(directory, GUARD_DIR)
		layer = self._file_layer(f"{base}/config.toml", directory)
		extra = self._toml(f"{base}/files.toml")
		if isinstance(extra, dict) and isinstance(extra.get("files"), list):
			valid = [
				item
				for item in extra["files"]
				if isinstance(item, dict) and isinstance(item.get("path"), str)
			]
			layer.files += files_from(valid, f"{base}/files.toml", directory)
		layer.rules_md = (self.reader(f"{base}/rules.md") or "").strip()
		return layer

	def _file_layer(self, path: str, directory: str) -> Layer:
		raw = self._toml(path)
		return normalize(raw if raw is not None else {}, path, directory)

	def _toml(self, path: str) -> Any:
		text = self.reader(path)
		if text is None:
			return None
		try:
			return tomllib.loads(text)
		except tomllib.TOMLDecodeError as error:
			self.errors.append(f"{path}: invalid TOML ({error})")
			return {}


def merge_auto_approve(policies: list[dict[str, Any]]) -> dict[str, Any]:
	if not policies:
		# Default: approve every clean 5/5 review, whatever the risk.
		return {"enabled": True, "risk_ceiling": "critical", "filters": {}}
	filters: dict[str, Any] = {}
	for policy in policies:
		for key, value in (policy.get("filters") or {}).items():
			if key.startswith(("exclude", "disabled", "ignore")):
				filters[key] = list(dict.fromkeys([*filters.get(key, []), *value]))
			elif key in filters:
				filters[key] = [x for x in filters[key] if x in value]
			else:
				filters[key] = list(value)
	return {
		"enabled": all(p.get("enabled", False) for p in policies),
		"risk_ceiling": min((p.get("risk_ceiling", "low") for p in policies), key=RISKS.index),
		"filters": filters,
	}


def tier_for(efforts: list[str], rule_tiers: list[str], requested: str | None) -> str:
	"""Highest of the configured tiers, matching tier rules and the request. `auto` counts as none."""
	explicit = [e for e in efforts if e in TIER_RANK and e != "auto"]
	configured = max(explicit, key=_rank) if explicit else "auto"
	rule_explicit = [t for t in rule_tiers if t and t != "auto"]
	if "auto" in rule_tiers and not rule_explicit:
		configured = "auto"
	candidates = [configured, *rule_explicit, *([requested] if requested and requested != "auto" else [])]
	return max(candidates, key=_rank)


def event_allowed(settings: dict[str, Any], event: str) -> bool:
	return event in EVENTS and EVENTS.index(event) < review_level(settings.get("auto_review") or [])


def is_config_path(path: str) -> bool:
	return f"/{GUARD_DIR}/" in f"/{path}" or posixpath.basename(path) == SINGLE_FILE


def _rank(tier: str) -> int:
	return TIER_RANK.get(tier, 0)


def _join(directory: str, name: str) -> str:
	return f"{directory}/{name}" if directory else name
