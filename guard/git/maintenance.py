"""Daily upkeep of the bare mirrors: drop the refs of closed pull requests and repack."""

import frappe

from guard.git.store import GitStore
from guard.utils import db

PULL_REFS = "refs/guard/pr/"


def run() -> None:
	"""One repository at a time, so the repacks never compete for the cores."""
	for name, clone_url in db.rows("SELECT name, clone_url FROM `tabCode Repository`"):
		store = GitStore(str(name), clone_url or "")
		if not (store.path / "HEAD").exists():
			continue
		try:
			maintain(store)
		except Exception as error:
			frappe.log_error(str(error), f"Guard git maintenance: {name}")


def maintain(store: GitStore) -> None:
	open_numbers = {
		str(number)
		for (number,) in db.rows(
			"SELECT number FROM `tabPull Request` WHERE repository = %s AND state = 'open'", (store.name,)
		)
	}
	store.delete_refs([ref for ref in store.refs(PULL_REFS) if ref[len(PULL_REFS) :] not in open_numbers])
	store.maintain()
