"""Links that Guard puts in comments."""

import hashlib
import hmac
from urllib.parse import quote

import frappe

from guard.settings import public_url


def sign(value: str) -> str:
	key = (frappe.conf.get("encryption_key") or frappe.local.site or "guard").encode()
	return hmac.new(key, f"guard:{value}".encode(), hashlib.sha256).hexdigest()[:32]


def verify(value: str, signature: str) -> bool:
	return hmac.compare_digest(sign(value), signature or "")


def retrigger(pull_request: str) -> str:
	return public_url(
		f"/api/method/guard.api.public.retrigger?pull_request={quote(pull_request)}&sig={sign(pull_request)}"
	)


def finding(name: str) -> str:
	return public_url(f"/app/review-finding/{quote(name)}")


def review_page(pull_request: str) -> str:
	return public_url(f"/app/guard-pr/{quote(pull_request)}")


def fix_all(pull_request: str) -> str:
	return public_url(f"/app/guard-pr/{quote(pull_request)}?fix=all")


def code_review(name: str) -> str:
	return public_url(f"/app/code-review/{quote(name)}")
