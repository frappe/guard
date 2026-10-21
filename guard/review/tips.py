"""Feature tips: one short hint the first time a feature can help. Off by default."""

from guard import settings as guard_settings
from guard.utils import db

CONFIG_TIP = "_Tip: teach Guard your team's rules for good with `.guard/rules.md` or `[[rules]]` in `.guard/config.toml`._"
FIX_TIP = "_Tip: fix every open finding at once with your coding agent: use **Fix all** in the summary, or run `guard fix install` once._"


def enabled() -> bool:
	return guard_settings.flag("feature_tips")


def after_reply(repository: str, author: str) -> str:
	"""For a person's first reply in a Guard thread of this repository."""
	if not enabled() or not author:
		return ""
	replies = db.scalar(
		"""SELECT COUNT(*) FROM `tabPull Request Comment` WHERE repository = %s AND author = %s
		AND IFNULL(finding, '') != '' AND is_guard = 0""",
		(repository, author),
	)
	return CONFIG_TIP if int(replies or 0) == 1 else ""


def in_summary(review_number: int) -> str:
	"""After repeated re-reviews of the same pull request."""
	return FIX_TIP if enabled() and review_number == 3 else ""
