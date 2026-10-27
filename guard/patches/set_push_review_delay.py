"""Give existing sites the push review delay of new ones.

A field added to a Single gets no value on migrate, and Frappe reads a missing Int as 0, which for
Push Review Delay means no delay.
"""

import frappe

from guard.settings import DEFAULT_PUSH_REVIEW_DELAY
from guard.utils import db


def execute():
	stored = db.scalar(
		"SELECT COUNT(*) FROM `tabSingles` WHERE doctype = 'Guard Settings' AND field = 'push_review_delay'"
	)
	if not stored:
		frappe.db.set_single_value("Guard Settings", "push_review_delay", DEFAULT_PUSH_REVIEW_DELAY)
	frappe.clear_document_cache("Guard Settings", "Guard Settings")
