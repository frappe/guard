"""Typed access to the Redis cache."""

from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any, cast

import frappe
from redis.exceptions import LockError


def get(key: str) -> Any:
	return _cache().get_value(key)


def set(key: str, value: Any, ttl: int) -> None:
	_cache().set_value(key, value, expires_in_sec=ttl)


def delete(key: str) -> None:
	_cache().delete_value(key)


@contextmanager
def exclusive(key: str, ttl: int) -> Iterator[bool]:
	"""Hold a lock while the block runs. Yield False, and do not wait, when another job holds it."""
	lock = _cache().lock(_cache().make_key(key), timeout=ttl, blocking=False)
	acquired = bool(lock.acquire())
	try:
		yield acquired
	finally:
		if acquired:
			# The lock can expire first, after `ttl`.
			with suppress(LockError):
				lock.release()


def _cache() -> Any:
	return cast(Any, frappe.cache)
