"""HTTP calls to the AI gateway with retries."""

import time
from typing import Any

import httpx

from guard.llm.types import GatewayBilling, GatewayBusy, GatewayDown, LLMError

RETRIES = 4
RATE_RETRIES = 3
RATE_WAIT_CAP = 30


def post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: int) -> dict[str, Any]:
	"""POST JSON. Retry network errors and 5xx. Raise GatewayBilling on 402, GatewayBusy on 429,
	and GatewayDown when the errors outlast the retries."""
	last_error: Exception | None = None
	for attempt in range(RETRIES + RATE_RETRIES):
		try:
			response = httpx.post(url, json=body, headers=headers, timeout=timeout)
		except httpx.HTTPError as error:
			last_error = error
			time.sleep(2**attempt)
			continue
		if response.status_code == 429 and attempt < RATE_RETRIES and not _in_test():
			# A short wait keeps the turns already done. Long waits go back to the job queue.
			time.sleep(min(_retry_after(response), RATE_WAIT_CAP) * (attempt + 1))
			continue
		if response.status_code == 402:
			raise GatewayBilling(_message(response), _retry_after(response))
		if response.status_code == 429:
			raise GatewayBusy(_message(response), _retry_after(response))
		if response.status_code >= 500:
			last_error = LLMError(_message(response))
			time.sleep(2**attempt)
			continue
		if response.status_code >= 400:
			raise LLMError(_message(response))
		return response.json()
	raise GatewayDown(f"Gateway request failed: {last_error}")


def get_json(url: str, headers: dict[str, str], timeout: int = 30) -> dict[str, Any]:
	try:
		response = httpx.get(url, headers=headers, timeout=timeout)
	except httpx.HTTPError as error:
		raise LLMError(f"Gateway request failed: {error}") from error
	if response.status_code >= 400:
		raise LLMError(_message(response))
	return response.json()


def _message(response: httpx.Response) -> str:
	text = response.text[:500]
	return f"Gateway answered {response.status_code}: {text}"


def _retry_after(response: httpx.Response) -> int:
	value = response.headers.get("Retry-After", "")
	return int(value) if value.isdigit() else (300 if response.status_code == 402 else 60)


def _in_test() -> bool:
	import frappe

	return bool(getattr(frappe.flags, "in_test", False))
