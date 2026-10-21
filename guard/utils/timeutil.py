"""Time helpers."""

from datetime import datetime

from frappe.utils import convert_utc_to_system_timezone, get_datetime


def parse_iso(value: str | None) -> datetime | None:
	"""An ISO 8601 time from GitHub, in the site time zone, without tzinfo."""
	if not value:
		return None
	moment = get_datetime(value.replace("Z", "+00:00"))
	if moment is None:
		return None
	if moment.tzinfo:
		moment = convert_utc_to_system_timezone(moment)
	return moment.replace(tzinfo=None)
