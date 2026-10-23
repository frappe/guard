from typing import Any

from frappe.tests import UnitTestCase

from guard.git.diff import parse_diff
from guard.llm.client import use_responder
from guard.review import agent, task
from guard.review.context import BuiltContext
from guard.review.prompt import ContextFile, ContextPack
from guard.review.run import ReviewRun
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
	def test_changed_files_show_their_hunks_only(self):
		run = run_on(LONG)
		run.files = parse_diff(DIFF)
		pack = ContextPack(
			files=[
				ContextFile("app/a.py", LONG, "changed file"),
				ContextFile("app/new.py", "print(1)", "changed file"),
				ContextFile("app/other_part.py", LONG, "changed file"),
			]
		)
		[block] = task.context_files(BuiltContext(pack), 10_000, task.around_hunks(run))
		self.assertIn("app/a.py", block)
		self.assertIn("line_500", block)
		self.assertNotIn("line_400 ", block)
		self.assertLess(len(block), 6000)
