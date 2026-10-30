"""Point MCP clients at the guide of local reviews on existing sites. New sites get it on install."""

from guard.install import add_agent_instructions


def execute():
	add_agent_instructions()
