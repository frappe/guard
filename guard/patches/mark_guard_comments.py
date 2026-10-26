"""Mark the stored comments that Guard wrote.

`bot_logins` did not include the login of the GitHub App, so a Guard reply with no `<!-- guard:`
marker was stored with `is_guard = 0`.
"""

import frappe

from guard import settings as guard_settings


def execute():
	logins = sorted(guard_settings.bot_logins())
	if logins:
		frappe.db.set_value(
			"Pull Request Comment",
			{"is_guard": 0, "author": ["in", logins]},
			"is_guard",
			1,
			update_modified=False,
		)
