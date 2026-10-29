"""The review agent: a tool loop over the LLM client."""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from guard.graph.queries import CodeGraph, Entity
from guard.llm.types import ChatResult, Message, ToolSpec
from guard.review.findings import Finding
from guard.review.prompts import FORCE_SUBMIT, TASK
from guard.review.run import ReviewRun, Verdict

MAX_REPORTED = 60
WARN_TURNS = 3
TURNS_LOW = (
	f"You have {WARN_TURNS} turns left. Stop exploring. Call report_finding for each issue you found, "
	"on the changed line it concerns, then call submit_review."
)
NUDGE = (
	"Not submitted. Your summary describes issues, but you reported no findings. "
	"Call report_finding once for each issue, on the changed line it concerns, then call submit_review again."
)
OUT_OF_TURNS = (
	"You have no turns left, and this tool did not run. In this answer, call report_finding for each "
	"issue you found, then submit_review."
)
# The tool choice of each call after the turn limit. A call that reads gets OUT_OF_TURNS, and the
# next call can still report. The last call forces the verdict.
CLOSE_CHOICES = ("required", "required", "submit_review")


def _mentions_issues(arguments: dict[str, Any]) -> bool:
	score = arguments.get("confidence_score")
	return isinstance(score, int | float) and score < 5


READ_LIMIT = 600
# Below the 8000 characters that a tool result keeps, so the line range in the header is true.
READ_CHARS = 7500
READ_RANGES = 6
SEARCH_LIMIT = 8
GREP_LIMIT = 40
CALLERS_SHOWN = 15
CALLEES_SHOWN = 40
DEFINITIONS_SHOWN = 10
LIST_LIMIT = 200
# Every turn sends the whole conversation again. Tool output older than this many turns is cut
# to its start: the model has read it, and can ask again.
KEEP_TURNS = 4
TRIM_AT = 600
# With a prompt cache, old tool output costs a few percent of its price, and a cut changes the
# prefix, so the cache misses from there on. Cut only past this size (about 60k tokens).
CACHED_HISTORY_CHARS = 240_000
TRIMMED = "\n[Older tool output cut short. Call the tool again if you need it.]"
TOOL_CHARS = 8000  # characters that one tool result keeps
# The tools of `read_specs`. Their answer for the same arguments does not change in a review.
READ_TOOLS = (
	"read_file",
	"list_directory",
	"grep",
	"search_code",
	"find_definition",
	"get_callers",
	"get_callees",
	"get_outline",
)
# A read that the agent asked for before gets a pointer to the earlier output, when that output is
# whole and longer than this: else the same text goes into the conversation twice, and every later
# turn sends it again.
SAME_MIN = 200
SAME = (
	"Same output as your {name} call in turn {turn}: it is above. Ask for other lines or names to read more."
)


@dataclass
class AgentResult:
	findings: list[Finding]
	verdict: Verdict
	turns: int


def read_specs(run: ReviewRun) -> list[ToolSpec]:
	"""The tools that read code and the graph. They change nothing."""
	line = {"type": "integer"}
	name = {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}
	specs = [
		ToolSpec(
			"read_file",
			"Read lines of a file at the head of the pull request, or at the base. Give `ranges` to read "
			+ f"up to {READ_RANGES} places of the file in one call.",
			{
				"type": "object",
				"properties": {
					"path": {"type": "string", "description": "Repository path"},
					"start_line": line,
					"end_line": line,
					"ranges": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {"start_line": line, "end_line": line},
							"required": ["start_line"],
						},
					},
					"ref": {"type": "string", "enum": ["head", "base"], "description": "Default: head"},
				},
				"required": ["path"],
			},
		),
		ToolSpec(
			"list_directory",
			"List the files and directories in a directory at the head.",
			{"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
		),
	]
	if run.grep:
		specs.append(
			ToolSpec(
				"grep",
				"Find lines that match an extended regular expression at the head (git grep).",
				{
					"type": "object",
					"properties": {
						"pattern": {"type": "string"},
						"path_glob": {"type": "string", "description": "For example '*.py' or 'app/**'"},
					},
					"required": ["pattern"],
				},
			)
		)
	if run.graph:
		specs += [
			ToolSpec(
				"search_code",
				"Search the indexed codebase by meaning or symbol name.",
				{"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
			),
			ToolSpec("find_definition", "Find where a symbol is defined, with its signature.", name),
			ToolSpec("get_callers", "List the callers of a symbol, each with the line of the call.", name),
			ToolSpec("get_callees", "List what a function or method calls.", name),
			ToolSpec(
				"get_outline",
				"List the functions, classes and imports defined in a file.",
				{"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
			),
		]
	return specs


def tools(run: ReviewRun) -> list[ToolSpec]:
	specs = [
		*read_specs(run),
		ToolSpec(
			"report_finding",
			"Report one review finding. Call again for each separate issue.",
			{
				"type": "object",
				"properties": {
					"path": {"type": "string", "description": "File path, preferably a changed file"},
					"start_line": {"type": "integer"},
					"end_line": {"type": "integer"},
					"severity": {"type": "string", "enum": ["P0", "P1", "P2"]},
					"comment_type": {"type": "string", "enum": ["logic", "syntax", "style"]},
					"category": {
						"type": "string",
						"description": "correctness, security, performance, style, test-gap",
					},
					"title": {"type": "string", "description": "At most 12 words, specific"},
					"body": {"type": "string", "description": "At most 3 sentences with evidence"},
					"suggestion": {
						"type": "string",
						"description": "Replacement code for the flagged lines, when local",
					},
					"question": {
						"type": "string",
						"description": "Clarifying question, when intent is unclear",
					},
					"rule_id": {"type": "string"},
					"confidence": {"type": "number"},
					"attach_last_run": {
						"type": "boolean",
						"description": "Attach the output of the last sandbox command as evidence",
					},
				},
				"required": ["path", "start_line", "severity", "comment_type", "title", "body"],
			},
		),
		ToolSpec(
			"submit_review",
			"Finish the review with the summary and confidence score.",
			{
				"type": "object",
				"properties": {
					"summary": {
						"type": "string",
						"description": "At most 4 short bullets on what the change does. No findings.",
					},
					"confidence_score": {"type": "integer", "minimum": 0, "maximum": 5},
					"confidence_reason": {"type": "string", "description": "One sentence"},
					"risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
					"risk_reason": {"type": "string", "description": "One sentence"},
					"diagram_type": {"type": "string", "enum": ["sequence", "er", "class", "flow", "none"]},
					"diagram": {"type": "string", "description": "Mermaid source"},
					"important_files": {
						"type": "array",
						"items": {
							"type": "object",
							"properties": {"path": {"type": "string"}, "why": {"type": "string"}},
							"required": ["path"],
						},
					},
				},
				"required": ["summary", "confidence_score"],
			},
		),
	]
	if run.sandbox is not None:
		specs += [
			ToolSpec(
				"run_in_sandbox",
				"Run a shell command in a container with the pull request checked out (for example the tests you wrote). No network.",
				{"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]},
			),
			ToolSpec(
				"write_sandbox_file",
				"Write a file in the sandbox work tree, for example a targeted test.",
				{
					"type": "object",
					"properties": {"path": {"type": "string"}, "content": {"type": "string"}},
					"required": ["path", "content"],
				},
			),
		]
	return specs


class Agent:
	"""One conversation with the reviewer model.

	The agent has its own verdict. A `final` agent writes it to the run: the verdict of an area
	of an apex review is not the verdict of the pull request.
	"""

	def __init__(
		self, run: ReviewRun, system: str, max_turns: int = 8, final: bool = True, label: str = "final"
	):
		self.run = run
		self.system = system
		self.max_turns = max_turns
		self.final = final
		# Names the conversation in the cache key, as each agent of a review has its own prompt.
		self.label = label
		self.verdict = Verdict()
		self.turns = 0
		self.done = False  # the model answered without a tool call
		self.messages: list[Message] = [Message("system", self.system, cache=True)]
		self.reported: set[str] = set()
		self.nudged = False
		self.closing = False  # out of turns: only report_finding and submit_review run
		self.tool_turns: list[list[int]] = []  # the message indexes of each turn's tool output
		self.caching = False  # the gateway read part of a prompt from its cache
		self.breakpoint: Message | None = None

	def run_loop(
		self, task: str | None, deadline: float = 0, check: Callable[[], None] | None = None
	) -> AgentResult:
		[result] = run_together([self], [task], check, deadline)
		return result

	@property
	def busy(self) -> bool:
		return self.turns < self.max_turns and not self.verdict.submitted and not self.done

	def request(self) -> list[Message]:
		"""Start the next turn: the messages to send."""
		self.turns += 1
		self.run.turns += 1
		if self.turns == self.max_turns - WARN_TURNS:
			self.messages.append(Message("user", TURNS_LOW))
		return self.messages

	def receive(self, result: ChatResult) -> None:
		"""End the turn: run the tool calls of the answer."""
		self.caching = self.caching or result.usage.cached_tokens > 0
		self.messages.append(result.as_message())
		if not result.tool_calls:
			self.done = True
			return
		indexes = []
		for call in result.tool_calls:
			indexes.append(len(self.messages))
			self.messages.append(self._dispatch(call.name, call.arguments, call.id))
		self.tool_turns.append(indexes)
		self._trim()
		self._move_breakpoint()

	def result(self) -> AgentResult:
		if self.final:
			self.run.verdict = self.verdict
		return AgentResult(self.run.findings, self.verdict, self.turns)

	def _trim(self) -> None:
		"""Cut the long tool output of the turns before the last `KEEP_TURNS`.

		When the gateway caches, the output stays until the conversation is large. Then all of it is
		cut at once, so the cache misses once and not on every turn.
		"""
		if self.caching and sum(len(m.content) for m in self.messages) < CACHED_HISTORY_CHARS:
			return
		for indexes in self.tool_turns[:-KEEP_TURNS]:
			for index in indexes:
				message = self.messages[index]
				if len(message.content) > TRIM_AT and not message.content.endswith(TRIMMED):
					message.content = message.content[:TRIM_AT] + TRIMMED

	def _move_breakpoint(self) -> None:
		"""Mark the newest tool output for the cache, so that the next turn reads the conversation
		up to here from the cache. Anthropic takes 4 marks: the system prompt, the task and this one."""
		if self.breakpoint:
			self.breakpoint.cache = False
		self.breakpoint = self.messages[-1]
		self.breakpoint.cache = True

	def cache_key(self) -> str:
		"""The key of this conversation for the prompt cache of the AI provider."""
		name = getattr(self.run.review, "name", None)
		return f"guard-review-{name}-{self.label}" if name else ""

	def _dispatch(self, name: str, args: dict[str, Any], call_id: str) -> Message:
		try:
			turn = self._earlier(name, args) if name in READ_TOOLS else None
			content = SAME.format(name=name, turn=turn) if turn else self._handle(name, args)
		except Exception as error:  # tool errors go back to the model
			content = f"error: {error}"
		content = content[:TOOL_CHARS]
		self.run.usage.tool(name, len(content))
		return Message("tool", content, tool_call_id=call_id)

	def _earlier(self, name: str, args: dict[str, Any]) -> int | None:
		"""The turn of an earlier call with the same arguments, when its output is still whole above.

		The outputs follow the calls of their turn in order, so an output pairs with the first open call
		of its id: test responders use the same ids in every turn.
		"""
		wanted = _call_key(name, args)
		turn, open_calls = 0, []
		for message in self.messages:
			if message.role == "assistant":
				turn += 1
				open_calls = [(call, turn) for call in message.tool_calls]
				continue
			if message.role != "tool":
				continue
			match = next((pair for pair in open_calls if pair[0].id == message.tool_call_id), None)
			if match is None:
				continue
			open_calls.remove(match)
			call, at = match
			content = message.content
			if (
				_call_key(call.name, call.arguments) == wanted
				and len(content) > SAME_MIN
				and not content.endswith(TRIMMED)
			):
				return at
		return None

	def _handle(self, name: str, args: dict[str, Any]) -> str:
		run = self.run
		if name == "report_finding":
			finding = Finding.from_arguments(args)
			fingerprint = finding.fingerprint
			if fingerprint in self.reported:
				return "already reported"
			self.reported.add(fingerprint)
			if len(run.findings) >= MAX_REPORTED:
				return "finding limit reached"
			if args.get("attach_last_run") and run.last_sandbox_output:
				finding.sandbox_log = run.last_sandbox_output
			run.findings.append(finding)
			return f"reported #{len(run.findings)}"
		if name == "submit_review":
			if not self.reported and not self.nudged and _mentions_issues(args):
				# Some models put every issue in the summary. Ask once for line findings.
				self.nudged = True
				return NUDGE
			self.verdict = Verdict.from_arguments(args)
			return "review submitted"
		if self.closing:
			return OUT_OF_TURNS
		answer = read_tool(run, name, args)
		if answer is not None:
			return answer
		if run.sandbox is not None and name == "write_sandbox_file":
			return run.sandbox.write(str(args.get("path") or ""), str(args.get("content") or ""))
		if run.sandbox is not None and name == "run_in_sandbox":
			result = run.sandbox.exec(str(args.get("command") or ""))
			run.last_sandbox_output = (
				f"$ {args.get('command')}\nexit {result['exit_code']}\n{result['output']}"
			)
			return run.last_sandbox_output
		return f"unknown tool {name}"


def run_together(
	agents: list[Agent],
	tasks: list[str | None],
	check: Callable[[], None] | None = None,
	deadline: float = 0,
) -> list[AgentResult]:
	"""Run the agents of one run side by side, each on its task.

	Each round sends one turn of every busy agent at once. The model calls run on threads, and the
	tool calls run here, one after the other, because they read `frappe.db`. `check` runs before
	each round, for example to stop a cancelled review. After `deadline` (a `time.time()`), no new
	round starts, and each agent must submit what it has. A task of None goes on with an agent
	that a checkpoint restored: it has its task already.
	"""
	from guard.llm.client import LLMClient

	if not agents:
		return []
	run = agents[0].run
	client = LLMClient(tier=run.tier, purpose="review", usage=run.usage)
	keys = {id(agent): agent.cache_key() for agent in agents}
	specs = tools(run)
	for agent, task in zip(agents, tasks, strict=True):
		if task is not None:
			agent.messages.append(Message("user", task, cache=True))
	while busy := [a for a in agents if a.busy]:
		if check:
			check()
		if deadline and time.time() > deadline:
			break
		run.rounds += 1
		results = client.chat_many(
			[a.request() for a in busy], tools=specs, cache_keys=[keys[id(a)] for a in busy]
		)
		for agent, result in zip(busy, results, strict=True):
			agent.receive(result)
	if client.online:
		_close([a for a in agents if not a.verdict.submitted], client, specs)
	return [agent.result() for agent in agents]


def _call_key(name: str, args: dict[str, Any]) -> str:
	return name + json.dumps(args, sort_keys=True, default=str)


def _close(agents: list[Agent], client: Any, specs: list[ToolSpec]) -> None:
	"""Out of turns: calls to report what is left, until a verdict comes. No nudge here.

	The calls send all the tools, and the agent refuses the ones that read. The prompt cache starts
	with the tools: with fewer tools, the whole conversation goes again at more than the full price.
	"""
	for agent in agents:
		agent.nudged = True
		agent.closing = True
		agent.messages.append(Message("user", FORCE_SUBMIT))
	for choice in CLOSE_CHOICES:
		left = [a for a in agents if not a.verdict.submitted]
		if not left:
			return
		results = client.chat_many(
			[a.messages for a in left],
			tools=specs,
			tool_choice=choice,
			cache_keys=[a.cache_key() for a in left],
		)
		for agent, result in zip(left, results, strict=True):
			agent.messages.append(result.as_message())
			for call in result.tool_calls:
				agent.messages.append(agent._dispatch(call.name, call.arguments, call.id))
			if not result.tool_calls:
				# The next request must end with a user turn, not with this answer.
				agent.messages.append(Message("user", FORCE_SUBMIT))


def read_tool(run: ReviewRun, name: str, args: dict[str, Any]) -> str | None:
	"""The answer to a call of one of `read_specs`, or None for another tool."""
	text = str(args.get("name") or args.get("query") or args.get("path") or "")
	if name == "read_file":
		return _read(run, text, args)
	if name == "list_directory":
		return _list(run, text)
	if name == "grep" and run.grep:
		return _grep(run, str(args.get("pattern") or ""), str(args.get("path_glob") or "") or None)
	graph = run.graph
	if not graph:
		return None
	if name == "search_code":
		return _search(graph, text)
	if name == "find_definition":
		return _definitions(graph, text)
	if name == "get_callers":
		return _callers(run, graph, text)
	if name == "get_callees":
		return _callees(graph, text)
	if name == "get_outline":
		return _outline(graph, text)
	return None


def _read(run: ReviewRun, path: str, args: dict[str, Any]) -> str:
	base = args.get("ref") == "base"
	text = run.base(path) if base else run.head(path)
	if text is None:
		return f"error: {path} not found at the {'base' if base else 'head'}"
	lines = text.splitlines()
	name = f"{path} at the base" if base else path
	blocks: list[str] = []
	room = READ_CHARS
	for start, end in _ranges(args):
		start, end = max(start, 1), min(max(end, start), len(lines))
		out: list[str] = []
		for number in range(start, end + 1):
			row = f"{number:5} {lines[number - 1]}"
			if out and len(row) + 1 > room:
				break
			out.append(row)
			room -= len(row) + 1
		last = start + len(out) - 1
		head = f"{name} lines {start}-{last} of {len(lines)}"
		if last < end:
			head += f". The output is full: read from line {last + 1} for more"
		blocks.append(head + "\n" + "\n".join(out))
		if last < end:
			break
	return "\n\n".join(blocks)


def _ranges(call: dict[str, Any]) -> list[tuple[int, int]]:
	"""The line ranges to read, in order, with overlaps joined."""
	items = call.get("ranges")
	items = items if isinstance(items, list) else []
	pairs = [_range(item) for item in items[:READ_RANGES] if isinstance(item, dict)] or [_range(call)]
	joined: list[tuple[int, int]] = []
	for start, end in sorted(pairs):
		if joined and start <= joined[-1][1] + 1:
			joined[-1] = (joined[-1][0], max(joined[-1][1], end))
		else:
			joined.append((start, end))
	return joined


def _range(span: dict[str, Any]) -> tuple[int, int]:
	start = _number(span.get("start_line"), 1)
	return start, _number(span.get("end_line"), start + READ_LIMIT - 1)


def _number(value: Any, default: int) -> int:
	try:
		return int(value) if value not in (None, "") else default
	except TypeError, ValueError:
		return default


def _list(run: ReviewRun, path: str) -> str:
	rows = sorted(run.list_dir(path.strip("/")))
	if not rows:
		return f"no entries in {path or '/'}"
	out = [f"{name}{'/' if kind == 'tree' else ''}" for name, kind in rows[:LIST_LIMIT]]
	if len(rows) > LIST_LIMIT:
		out.append(f"... {len(rows) - LIST_LIMIT} more")
	return "\n".join(out)


def _grep(run: ReviewRun, pattern: str, path_glob: str | None) -> str:
	if not pattern or not run.grep:
		return "error: give a pattern"
	hits = run.grep(pattern, path_glob)
	out = [f"{path}:{line}: {text.strip()[:200]}" for path, line, text in hits[:GREP_LIMIT]]
	if len(hits) > GREP_LIMIT:
		out.append("... more matches: narrow the pattern or the path_glob")
	return "\n".join(out) or "no matches"


def _search(graph: CodeGraph, query: str) -> str:
	# `search` already ranks the FULLTEXT hits with the others.
	hits = graph.search(query, k=SEARCH_LIMIT)
	seen: set[tuple[str, int]] = set()
	lines = []
	for hit in hits:
		key = (hit.path, hit.start_line)
		if key in seen:
			continue
		seen.add(key)
		lines.append(
			f"{hit.path}:{hit.start_line}-{hit.end_line} {hit.entity or hit.name}\n  {hit.content[:300]}"
		)
		if len(lines) >= SEARCH_LIMIT:
			break
	return "\n".join(lines) or "no results"


def _definitions(graph: CodeGraph, name: str) -> str:
	rows = graph.definitions(name, limit=DEFINITIONS_SHOWN)
	return (
		"\n".join(
			f"{e.path}:{e.start_line}-{e.end_line} {e.kind} {e.qualified_name}\n    {(e.signature or '')[:200]}"
			for e in rows
		)
		or f"no definition found for {name}"
	)


def _symbol(graph: CodeGraph, name: str) -> Entity | None:
	return graph.entity(name) or next(iter(graph.definitions(name)), None)


def _callers(run: ReviewRun, graph: CodeGraph, name: str) -> str:
	"""The callers of `name`, each with the line of the call, so the model need not read it."""
	entity = _symbol(graph, name)
	if not entity:
		return f"no definition found for {name}"
	links = graph.callers(entity, limit=CALLERS_SHOWN)
	if not links:
		return f"no callers of {name}"
	# The index is of the base branch, so the line numbers are too.
	changed = set(run.paths)
	sources: dict[str, list[str]] = {}
	out = [f"Callers of {entity.label}. Line numbers are at the base."]
	for link in links:
		if link.path not in sources:
			sources[link.path] = (run.base(link.path) or run.head(link.path) or "").splitlines()
		lines = sources[link.path]
		call = lines[link.line - 1].strip()[:200] if 0 < link.line <= len(lines) else ""
		caller = link.entity.qualified_name if link.entity else link.name
		note = " (the pull request changes this file)" if link.path in changed else ""
		out.append(f"{link.path}:{link.line} in {caller}{note}\n    {call}")
	return "\n".join(out)


def _callees(graph: CodeGraph, name: str) -> str:
	entity = _symbol(graph, name)
	if not entity:
		return f"no definition found for {name}"
	links = graph.callees(entity, limit=CALLEES_SHOWN)
	return (
		"\n".join(
			f"{link.line:5} {link.text or link.name} -> {link.entity.label if link.entity else 'not in the index'}"
			for link in links
		)
		or f"{name} calls nothing in the index"
	)


def _outline(graph: CodeGraph, path: str) -> str:
	rows = graph.outline(path)
	if not rows:
		return f"no index data for {path}"
	return "\n".join(
		f"{e.kind:9} {e.start_line:5}-{e.end_line:<5} {e.qualified_name}{(' (entry)' if e.is_entry_point else '')}"
		for e in rows
	)


TASK_TEXT = TASK
