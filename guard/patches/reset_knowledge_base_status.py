"""Give back the Ready status to knowledge bases that a stale rebuild left Building or Failed.

The stale sync rebuilt documents inside a scheduled job with a 5-minute timeout. A rebuild that
timed out set the repository to Failed, and one that the worker killed left it at Building. Pushes
then stopped marking documents stale, and MCP stopped reading them. A repository with a finished
full build (`kb_version`) still has its documents.
"""

import frappe

from guard.knowledge.builder import recover_stuck
from guard.utils import db


def execute():
	recover_stuck()
	for name in frappe.get_all(
		"Code Repository", filters={"kb_status": "Failed", "kb_version": ["is", "set"]}, pluck="name"
	):
		db.set_values("Code Repository", name, {"kb_status": "Ready"})
