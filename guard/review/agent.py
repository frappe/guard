"""The review agent: a tool loop over the LLM client."""

from dataclasses import dataclass
from typing import Any

from guard.graph.queries import CodeGraph
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


def _mentions_issues(args: dict[str, Any]) -> bool:
	score = args.get("confidence_score")
	return isinstance(score, int | float) and score < 5


READ_LIMIT = 600
SEARCH_LIMIT = 8


@dataclass
class AgentResult:
	findings: list[Finding]
	verdict: Verdict
	turns: int


def tools(run: ReviewRun) -> list[ToolSpec]:
	specs = [
		ToolSpec(
			"read_file",
			"Read a file from the pull request branch. Optional line range.",
			{
				"type": "object",
				"properties": {
					"path": {"type": "string", "description": "Repository path"},
					"start_line": {"type": "integer"},
					"end_line": {"type": "integer"},
				},
				"required": ["path"],
			},
		),
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
					"title": {"type": "string", "description": "One line, specific"},
					"body": {"type": "string", "description": "Markdown explanation with evidence"},
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
						"description": "Markdown: what the change does, walkthrough, concerns",
					},
					"confidence_score": {"type": "integer", "minimum": 0, "maximum": 5},
					"confidence_reason": {"type": "string"},
					"risk_level": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
					"risk_reason": {"type": "string"},
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
	graph = run.graph
	if graph:
		specs += [
			ToolSpec(
				"search_code",
				"Search the indexed codebase by meaning or symbol name.",
				{
					"type": "object",
					"properties": {"query": {"type": "string"}},
					"required": ["query"],
				},
			),
			ToolSpec(
				"get_callers",
				"List the callers of a symbol in the indexed codebase.",
				{"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
			),
			ToolSpec(
				"get_outline",
				"List the functions, classes and imports defined in a file.",
				{"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
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
	"""One conversation with the reviewer model."""

	def __init__(self, run: ReviewRun, system: str, max_turns: int = 8):
		self.run = run
		self.system = system
		self.max_turns = max_turns
		self.messages: list[Message] = [Message("system", self.system, cache=True)]
		self.reported: set[str] = set()
		self.nudged = False

	def run_loop(self, task: str) -> AgentResult:
		from guard.llm.client import LLMClient

		client = LLMClient(tier=self.run.tier, purpose="review", usage=self.run.usage)
		specs = tools(self.run)
		self.messages.append(Message("user", task))
		turns = 0
		while turns < self.max_turns and not self.run.verdict.submitted:
			turns += 1
			if turns == self.max_turns - WARN_TURNS:
				self.messages.append(Message("user", TURNS_LOW))
			result = client.chat(self.messages, tools=specs)
			self.messages.append(result.as_message())
			if not result.tool_calls:
				break
			for call in result.tool_calls:
				self.messages.append(self._dispatch(call.name, call.arguments, call.id))
		if not self.run.verdict.submitted and client.online:
			self._close(client, specs)
		return AgentResult(self.run.findings, self.run.verdict, turns)

	def _close(self, client, specs: list[ToolSpec]) -> None:
		"""Out of turns: one call to report what is left, then one forced verdict. No nudge here."""
		self.nudged = True
		final_tools = [s for s in specs if s.name in ("report_finding", "submit_review")]
		self.messages.append(Message("user", FORCE_SUBMIT))
		result = client.chat(self.messages, tools=final_tools, tool_choice="required")
		self.messages.append(result.as_message())
		for call in result.tool_calls:
			self.messages.append(self._dispatch(call.name, call.arguments, call.id))
		if not self.run.verdict.submitted:
			verdict = client.chat(
				self.messages,
				tools=[s for s in specs if s.name == "submit_review"],
				tool_choice="submit_review",
			)
			for call in verdict.tool_calls:
				self._dispatch(call.name, call.arguments, call.id)

	def _dispatch(self, name: str, args: dict[str, Any], call_id: str) -> Message:
		try:
			content = self._handle(name, args)
		except Exception as error:  # tool errors go back to the model
			content = f"error: {error}"
		return Message("tool", content[:8000], tool_call_id=call_id)

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
			run.verdict = Verdict.from_arguments(args)
			return "review submitted"
		if name == "read_file":
			return _read(run, str(args.get("path") or ""), args)
		if run.sandbox is not None and name == "write_sandbox_file":
			return run.sandbox.write(str(args.get("path") or ""), str(args.get("content") or ""))
		if run.sandbox is not None and name == "run_in_sandbox":
			result = run.sandbox.exec(str(args.get("command") or ""))
			run.last_sandbox_output = (
				f"$ {args.get('command')}\nexit {result['exit_code']}\n{result['output']}"
			)
			return run.last_sandbox_output
		if run.graph:
			if name == "search_code":
				return _search(run.graph, str(args.get("query") or ""))
			if name == "get_callers":
				return _callers(run.graph, str(args.get("name") or ""))
			if name == "get_outline":
				return _outline(run.graph, str(args.get("path") or ""))
		return f"unknown tool {name}"


def _read(run: ReviewRun, path: str, args: dict[str, Any]) -> str:
	text = run.head(path)
	if text is None:
		return f"error: {path} not found"
	lines = text.splitlines()
	start = int(args.get("start_line") or 1)
	end = int(args.get("end_line") or min(start + READ_LIMIT - 1, len(lines)))
	start, end = max(start, 1), max(end, start)
	chunk = lines[start - 1 : end]
	head = f"{path} lines {start}-{end} of {len(lines)}"
	return head + "\n" + "\n".join(f"{n:5} {line}" for n, line in enumerate(chunk, start))


def _search(graph: CodeGraph, query: str) -> str:
	hits = graph.search(query, k=SEARCH_LIMIT) + graph._lexical(query, k=SEARCH_LIMIT)
	seen: set[tuple[str, int]] = set()
	lines = []
	for hit in sorted(hits, key=lambda h: -h.score):
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


def _callers(graph: CodeGraph, name: str) -> str:
	entity = graph.entity(name) or next(iter(graph.definitions(name)), None)
	if not entity:
		return f"no definition found for {name}"
	links = graph.callers(entity, limit=15)
	return (
		"\n".join(
			f"{link.path}:{link.line} {link.entity.label if link.entity else link.name}" for link in links
		)
		or f"no callers of {name}"
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
