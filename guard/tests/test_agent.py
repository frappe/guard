from typing import Any, cast
from unittest.mock import patch

from frappe.tests import UnitTestCase

from guard.git.diff import parse_diff
from guard.graph.queries import CodeGraph, Entity, Link
from guard.llm.client import ChatRequest, use_responder
from guard.llm.types import ChatResult
from guard.review import agent, context, task
from guard.review.context import BuiltContext
from guard.review.prompt import ContextFile, ContextPack
from guard.review.run import ReviewRun
from guard.review.spans import ContextSpan
from guard.tests.fakes import dsml, scripted

LONG = "\n".join(f"line_{n} = {'x' * 60}" for n in range(1, 1001))

DIFF = """diff --git a/app/a.py b/app/a.py
--- a/app/a.py
+++ b/app/a.py
@@ -500,2 +500,2 @@
 line_499 = x
-line_500 = x
+line_500 = y
diff --git a/app/new.py b/app/new.py
new file mode 100644
--- /dev/null
+++ b/app/new.py
@@ -0,0 +1 @@
+print(1)
"""


def run_on(text: str) -> ReviewRun:
	return ReviewRun(None, None, "", lambda path: text, lambda path: None, lambda path: [], "", "")


class TestRead(UnitTestCase):
	def test_read_stops_at_the_character_budget_and_says_where(self):
		out = agent._read(run_on(LONG), "app/a.py", {})
		self.assertLessEqual(len(out), 8000)
		last = int(out.splitlines()[-1].split()[0])
		self.assertIn(f"lines 1-{last} of 1000", out)
		self.assertIn(f"read from line {last + 1}", out)

	def test_short_read_has_no_cut_note(self):
		out = agent._read(run_on(LONG), "app/a.py", {"start_line": 10, "end_line": 12})
		self.assertTrue(out.startswith("app/a.py lines 10-12 of 1000\n"))
		self.assertNotIn("for more", out)

	def test_one_call_reads_several_ranges_and_joins_overlaps(self):
		ranges = [
			{"start_line": 900, "end_line": 901},
			{"start_line": 10, "end_line": 12},
			{"start_line": 12, "end_line": 14},
		]
		out = agent._read(run_on(LONG), "app/a.py", {"ranges": ranges})
		self.assertEqual(
			[line for line in out.splitlines() if line.startswith("app/a.py")],
			["app/a.py lines 10-14 of 1000", "app/a.py lines 900-901 of 1000"],
		)

	def test_read_at_the_base(self):
		run = run_on(LONG)
		run.base = lambda path: "old = 1"
		out = agent.read_tool(run, "read_file", {"path": "app/a.py", "ref": "base"})
		self.assertEqual(out, "app/a.py at the base lines 1-1 of 1\n    1 old = 1")


class FakeGraph:
	def __init__(self, target: Entity, links: list[Link]):
		self.target, self.links = target, links

	def entity(self, name: str) -> Entity | None:
		return None

	def definitions(self, name: str, limit: int = 20) -> list[Entity]:
		return [self.target] if name == self.target.entity_name else []

	def callers(self, entity: Entity, limit: int = 50) -> list[Link]:
		return self.links

	def callees(self, entity: Entity, limit: int = 100) -> list[Link]:
		return [Link(None, "helper", "self.helper", entity.path, 3, False)]


class TestGraphTools(UnitTestCase):
	def setUp(self):
		target = Entity("t", "function", "f", "m.f", "m.py", None, 1, 5, "def f(a, b)", "python")
		caller = Entity("c", "function", "g", "app.a.g", "app/a.py", None, 400, 420, "def g()", "python")
		self.review = run_on(LONG)
		self.review.base = lambda path: LONG if path == "app/a.py" else None
		self.review.files = parse_diff(DIFF)
		self.review.graph = cast(
			CodeGraph, FakeGraph(target, [Link(caller, "f", "f", "app/a.py", 410, True)])
		)

	def test_callers_show_the_line_of_each_call(self):
		out = agent.read_tool(self.review, "get_callers", {"name": "f"}) or ""
		self.assertIn("app/a.py:410 in app.a.g (the pull request changes this file)\n    line_410 = ", out)
		self.assertEqual(
			agent.read_tool(self.review, "get_callers", {"name": "nope"}), "no definition found for nope"
		)

	def test_definitions_and_callees(self):
		self.assertIn(
			"m.py:1-5 function m.f\n    def f(a, b)",
			agent.read_tool(self.review, "find_definition", {"name": "f"}) or "",
		)
		self.assertEqual(
			agent.read_tool(self.review, "get_callees", {"name": "f"}),
			"    3 self.helper -> not in the index",
		)

	def test_grep_is_a_tool_only_with_a_repository(self):
		self.assertNotIn("grep", [spec.name for spec in agent.tools(self.review)])
		self.review.grep = lambda pattern, glob: [("app/a.py", 7, f"  {pattern} {glob}  ")]
		self.assertIn("grep", [spec.name for spec in agent.tools(self.review)])
		out = agent.read_tool(self.review, "grep", {"pattern": "def f", "path_glob": "*.py"})
		self.assertEqual(out, "app/a.py:7: def f *.py")

	def test_the_answerer_has_the_read_tools_only(self):
		names = [spec.name for spec in agent.read_specs(self.review)]
		self.assertIn("get_callees", names)
		self.assertNotIn("report_finding", names)


class TestToolCallsAsText(UnitTestCase):
	def test_the_review_goes_on_after_tool_calls_that_came_back_as_text(self):
		review = agent.Agent(run_on(LONG), "system", max_turns=4)
		with use_responder(scripted([dsml([("read_file", {"path": "app/a.py"})])])):
			review.run_loop("task")
		self.assertTrue(review.run.verdict.submitted)
		self.assertIn("line_1 = ", next(m.content for m in review.messages if m.role == "tool"))


class TestTrim(UnitTestCase):
	def test_tool_output_of_older_turns_is_cut(self):
		turns = agent.KEEP_TURNS + 2
		steps: list[list[tuple[str, dict[str, Any]]] | str] = [
			[("read_file", {"path": "app/a.py", "start_line": 1 + 100 * n})] for n in range(turns)
		]
		review = agent.Agent(run_on(LONG), "system", max_turns=turns + 1)
		with use_responder(scripted(steps)):
			review.run_loop("task")
		outputs = [m.content for m in review.messages if m.role == "tool"][:turns]
		self.assertTrue(review.run.verdict.submitted)
		# The turn of submit_review is the last of the turns with tool output.
		cut = turns + 1 - agent.KEEP_TURNS
		for content in outputs[:cut]:
			self.assertTrue(content.endswith(agent.TRIMMED))
		for content in outputs[cut:]:
			self.assertNotIn(agent.TRIMMED, content)

	def cached_review(self) -> agent.Agent:
		turns = agent.KEEP_TURNS + 2
		steps: list[list[tuple[str, dict[str, Any]]] | str] = [
			[("read_file", {"path": "app/a.py", "start_line": 1 + 100 * n})] for n in range(turns)
		]
		plain = scripted(steps)

		def respond(request: ChatRequest) -> ChatResult:
			result = plain(request)
			result.usage.cached_tokens = 50
			return result

		review = agent.Agent(run_on(LONG), "system", max_turns=turns + 1)
		with use_responder(respond):
			review.run_loop("task")
		return review

	def test_a_caching_gateway_keeps_the_prefix(self):
		review = self.cached_review()
		self.assertTrue(review.caching)
		self.assertFalse(any(agent.TRIMMED in m.content for m in review.messages))
		marked = [m.role for m in review.messages if m.cache]
		self.assertEqual(marked, ["system", "user", "tool"], "one mark moves with the newest output")
		self.assertIs(review.messages[-1], review.breakpoint)

	def test_a_large_cached_conversation_is_cut_all_at_once(self):
		with patch.object(agent, "CACHED_HISTORY_CHARS", 30_000):
			review = self.cached_review()
		outputs = [m.content for m in review.messages if m.role == "tool"]
		cut = [agent.TRIMMED in content for content in outputs]
		self.assertTrue(any(cut))
		self.assertEqual(cut, sorted(cut, reverse=True), "older output first")
		self.assertFalse(any(cut[-agent.KEEP_TURNS :]))


class TestChangedFiles(UnitTestCase):
	def test_the_diff_shows_the_lines_around_each_hunk_once(self):
		run = run_on(LONG)
		run.files = parse_diff(DIFF)
		diff = task.annotated_diff(run)
		# The hunk covers head lines 500-501.
		first, last = 500 - task.HUNK_PAD, 501 + task.HUNK_PAD
		self.assertIn(f"{first:>5}   line_{first} =", diff)
		self.assertNotIn(f"line_{first - 1} =", diff)
		self.assertIn(f"{last:>5}   line_{last} =", diff)
		self.assertNotIn(f"line_{last + 1} =", diff)
		self.assertIn("    1 + print(1)", diff, "an added file is all in the diff")

	def test_a_large_diff_gets_fewer_lines_around_its_hunks(self):
		run = run_on(LONG)
		run.files = parse_diff(DIFF)
		plain = "\n\n".join(f.annotated() for f in run.files)
		wide = task.annotated_diff(run)
		narrow = task.annotated_diff(run, len(wide) - len(plain) - 1)
		self.assertIn(f"line_{501 + task.HUNK_PADS[1]} =", narrow)
		self.assertNotIn(f"line_{502 + task.HUNK_PADS[1]} =", narrow)
		self.assertEqual(task.annotated_diff(run, 1), plain)

	def test_close_hunks_share_their_context(self):
		text = "\n".join(f"l{n}" for n in range(1, 101))
		diff = """diff --git a/a.py b/a.py
--- a/a.py
+++ b/a.py
@@ -10 +10 @@
-x
+l10
@@ -30 +30 @@
-y
+l30
@@ -90 +90 @@
-z
+l90
"""
		[file] = parse_diff(diff)
		out = file.annotated(text.splitlines(), 5)
		numbers = [int(line[:5]) for line in out.splitlines() if line[:5].strip().isdigit()]
		self.assertEqual(numbers, sorted(set(numbers)), "no head line twice, in order")
		self.assertEqual(numbers[:6], [5, 6, 7, 8, 9, 10])
		self.assertIn(35, numbers)
		self.assertNotIn(36, numbers)
		self.assertIn(85, numbers)
		self.assertEqual(numbers[-1], 95)

	def test_context_files_show_their_spans_only(self):
		pack = ContextPack(files=[ContextFile("app/b.py", LONG, "caller of f")])
		built = BuiltContext(pack, {"app/b.py": [ContextSpan(400, 402, "call")]})
		[block] = task.context_files(built, 10_000)
		self.assertIn("line_401 ", block)
		self.assertNotIn("line_399 ", block)

	def test_a_caller_shows_its_first_line_and_the_call(self):
		entity = Entity(
			"e", "function", "caller", "m.caller", "app/b.py", None, 100, 400, "def caller()", "python"
		)
		call = Link(entity, "f", "f()", "app/b.py", 300, True)
		spans = context._link_spans(call, "caller of f", whole=False)
		self.assertEqual([(s.start, s.end) for s in spans], [(100, 100), (290, 310)])
		similar = context._link_spans(Link(entity, "", "", "app/b.py", 100, True), "similar", whole=True)
		self.assertEqual(similar[0].end, 100 + context.SIMILAR_LINES + 3)
