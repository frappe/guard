"""Keep the prices of existing sites: the one set of prices becomes the prices of the base tier.

The plus and apex tiers then use them too, until an admin sets their own.
"""

from frappe.model.utils.rename_field import rename_field

FIELDS = ("input_price", "cached_input_price", "output_price")


def execute():
	for name in FIELDS:
		rename_field("Guard Settings", name, f"base_{name}")
