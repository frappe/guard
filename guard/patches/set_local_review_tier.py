"""Give local reviews the tier `local`, and link the open pull request of their branch.

It links the pull request that is open now. A local review of a branch whose pull request has ended
stays without one.
"""

from typing import Any

import frappe

from guard.review.local import open_pull_request
from guard.utils import db


def execute():
	reviews = frappe.get_all(
		"Code Review",
		filters={"source": "local"},
		fields=["name", "repository", "branch", "pull_request"],
	)
	for review in reviews:
		values: dict[str, Any] = {"tier": "local"}
		if not review.pull_request and review.repository:
			values["pull_request"] = open_pull_request(str(review.repository), str(review.branch or ""))
		db.set_values("Code Review", str(review.name), values)
