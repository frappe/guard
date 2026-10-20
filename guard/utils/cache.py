"""Typed access to the Redis cache."""

from typing import Any, cast

import frappe


def get(key: str) -> Any:
	return _cache().get_value(key)


def set(key: str, value: Any, ttl: int) -> None:
	_cache().set_value(key, value, expires_in_sec=ttl)


def delete(key: str) -> None:
	_cache().delete_value(key)


def _cache() -> Any:
	return cast(Any, frappe.cache)
