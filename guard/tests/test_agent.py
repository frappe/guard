from typing import Any

from frappe.tests import UnitTestCase

from guard.git.diff import parse_diff
from guard.graph.queries import Entity, Link
from guard.llm.client import use_responder
from guard.review import agent, context, task
from guard.review.context import BuiltContext
from guard.review.prompt import ContextFile, ContextPack
from guard.review.run import ReviewRun
from guard.review.spans import ContextSpan
from guard.tests.fakes import scripted

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
