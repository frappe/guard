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


# The hash helpers send plain Redis commands: Frappe's own hash methods keep a copy for the request,
# and its `hdel` does not tell whether the field was there.


def hash_set(name: str, field: str, value: str, ttl: int) -> None:
	"""Set one field of a hash. The hash expires `ttl` seconds after its last change."""
	key = _cache().make_key(name)
	pipeline = _cache().pipeline()
	pipeline.hset(key, field, value)
	pipeline.expire(key, ttl)
	pipeline.execute()


def hash_items(name: str) -> dict[str, str]:
	pipeline = _cache().pipeline()
	pipeline.hgetall(_cache().make_key(name))
	[items] = pipeline.execute()
	return {field.decode(): value.decode() for field, value in items.items()}


def hash_take(name: str, field: str) -> bool:
	"""Remove one field of a hash. Only the caller that removed it gets True."""
	pipeline = _cache().pipeline()
	pipeline.hdel(_cache().make_key(name), field)
	[removed] = pipeline.execute()
	return bool(removed)


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
