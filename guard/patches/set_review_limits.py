"""Give existing sites the review limits of new ones.

A field added to a Single gets no value on migrate, and Frappe reads a missing Int as 0, which
for Max Concurrent Reviews means no limit. Turns still at the old defaults move to the new ones.
"""

import frappe

from guard.settings import DEFAULT_CONCURRENT_REVIEWS, DEFAULT_TURNS
from guard.utils import db

OLD_TURNS = {"base": 15, "plus": 30, "apex": 50}


def execute():
	stored = {row[0] for row in db.rows("SELECT field FROM `tabSingles` WHERE doctype = 'Guard Settings'")}
	if "max_concurrent_reviews" not in stored:
		frappe.db.set_single_value("Guard Settings", "max_concurrent_reviews", DEFAULT_CONCURRENT_REVIEWS)
	for tier, old in OLD_TURNS.items():
		field = f"max_turns_{tier}"
		if frappe.db.get_single_value("Guard Settings", field) in (old, 0, None):
			frappe.db.set_single_value("Guard Settings", field, DEFAULT_TURNS[tier])
	frappe.clear_document_cache("Guard Settings", "Guard Settings")
