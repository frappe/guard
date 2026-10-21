"""Jira (with Confluence) and Linear issue context. Read-only."""

import re
from dataclasses import dataclass
from typing import Any

import frappe
import httpx

from guard.utils.redact import redact

KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,7})\b")
JIRA_LINK = re.compile(r"https?://[\w.-]+/browse/([A-Z][A-Z0-9]+-\d+)")
LINEAR_LINK = re.compile(r"https?://linear\.app/[\w-]+/issue/([A-Za-z][A-Za-z0-9]+-\d+)")
LINEAR_API = "https://api.linear.app/graphql"
MAX_TICKETS = 5


@dataclass
class Ticket:
	key: str
	source: str  # Jira, Linear, Confluence
	title: str
	url: str
	status: str = ""
	body: str = ""
	comments: str = ""
	from_search: bool = False

	def as_text(self) -> str:
		parts = [
			f"{self.source} {self.key}: {self.title}",
			f"Status: {self.status}",
			f"URL: {self.url}",
			self.body[:4000],
		]
		if self.comments:
			parts.append("Comments:\n" + self.comments[:2000])
		return "\n".join(p for p in parts if p)


def for_pull_request(title: str, description: str, branch: str) -> list[Ticket]:
	"""Tickets named in the title, the description or the branch."""
	keys = find_keys(f"{title}\n{description}\n{branch.replace('_', '-')}")
	tickets = []
	for key in keys[:MAX_TICKETS]:
		ticket = lookup(key)
		if ticket:
			tickets.append(ticket)
	return tickets


def find_keys(text: str) -> list[str]:
	keys = [m.upper() for m in JIRA_LINK.findall(text) + LINEAR_LINK.findall(text)]
	keys += [m.upper() for m in KEY.findall(text.upper())]
	return list(dict.fromkeys(k for k in keys if not k.startswith(("HTTP-", "UTF-", "SHA-", "ISO-", "RFC-"))))


def lookup(key: str) -> Ticket | None:
	for integration in _integrations():
		client = CLIENTS.get(integration.integration_type)
		if not client:
			continue
		try:
			ticket = client(integration).issue(key)
		except Exception as error:
			frappe.log_error(redact(str(error)), "Guard ticket lookup")
			continue
		if ticket:
			return ticket
	return None


def search(query: str, limit: int = 5) -> list[Ticket]:
	results: list[Ticket] = []
	for integration in _integrations():
		client = CLIENTS.get(integration.integration_type)
		if client:
			try:
				results += client(integration).search(query, limit)
			except Exception as error:
				frappe.log_error(redact(str(error)), "Guard ticket search")
	return results[:limit]


def instructions() -> str:
	return "\n".join(i.instructions for i in _integrations() if i.get("instructions"))


def _integrations() -> list[Any]:
	names = frappe.get_all("Guard Integration", filters={"enabled": 1}, pluck="name")
	return [frappe.get_cached_doc("Guard Integration", name) for name in names]


class JiraClient:
	def __init__(self, doc: Any):
		self.site = (doc.get("site_url") or "").rstrip("/")
		token = doc.get_password("api_token", raise_exception=False) or ""
		self.auth = (doc.get("email") or "", token)

	def issue(self, key: str) -> Ticket | None:
		response = httpx.get(
			f"{self.site}/rest/api/3/issue/{key}",
			params={"fields": "summary,description,status,issuetype,comment"},
			auth=self.auth,
			timeout=20,
		)
		if response.status_code != 200:
			return None
		fields = response.json().get("fields") or {}
		comments = (fields.get("comment") or {}).get("comments") or []
		return Ticket(
			key=key,
			source="Jira",
			title=fields.get("summary") or "",
			url=f"{self.site}/browse/{key}",
			status=(fields.get("status") or {}).get("name") or "",
			body=adf_text(fields.get("description")),
			comments="\n".join(adf_text(c.get("body")) for c in comments[-10:]),
		)

	def search(self, query: str, limit: int) -> list[Ticket]:
		jql = f'text ~ "{query.replace(chr(34), "")}" ORDER BY updated DESC'
		response = httpx.get(
			f"{self.site}/rest/api/3/search/jql",
			params={"jql": jql, "maxResults": limit, "fields": "summary,status"},
			auth=self.auth,
			timeout=20,
		)
		tickets = []
		if response.status_code == 200:
			for item in response.json().get("issues") or []:
				fields = item.get("fields") or {}
				tickets.append(
					Ticket(
						item["key"],
						"Jira",
						fields.get("summary") or "",
						f"{self.site}/browse/{item['key']}",
						(fields.get("status") or {}).get("name") or "",
						from_search=True,
					)
				)
		cql = f'text ~ "{query.replace(chr(34), "")}"'
		pages = httpx.get(
			f"{self.site}/wiki/rest/api/search",
			params={"cql": cql, "limit": limit},
			auth=self.auth,
			timeout=20,
		)
		if pages.status_code == 200:
			for item in pages.json().get("results") or []:
				content = item.get("content") or {}
				url = f"{self.site}/wiki{(item.get('url') or '')}"
				tickets.append(
					Ticket(
						str(content.get("id") or ""),
						"Confluence",
						item.get("title") or "",
						url,
						body=re.sub(r"@@@\w*@@@", "", item.get("excerpt") or ""),
						from_search=True,
					)
				)
		return tickets


class LinearClient:
	def __init__(self, doc: Any):
		self.key = doc.get_password("api_token", raise_exception=False) or ""
		self.teams = {t.strip().upper() for t in (doc.get("linear_teams") or "").splitlines() if t.strip()}

	def issue(self, key: str) -> Ticket | None:
		if self.teams and key.split("-")[0] not in self.teams:
			return None
		query = "query($id: String!) { issue(id: $id) { identifier title description url state { name } comments(last: 10) { nodes { body } } } }"
		data = self._query(query, {"id": key})
		issue = (data or {}).get("issue")
		if not issue:
			return None
		return Ticket(
			key=issue["identifier"],
			source="Linear",
			title=issue.get("title") or "",
			url=issue.get("url") or "",
			status=(issue.get("state") or {}).get("name") or "",
			body=issue.get("description") or "",
			comments="\n".join(n.get("body") or "" for n in (issue.get("comments") or {}).get("nodes") or []),
		)

	def search(self, query: str, limit: int) -> list[Ticket]:
		text = "query($term: String!, $first: Int!) { searchIssues(term: $term, first: $first) { nodes { identifier title url state { name } } } }"
		data = self._query(text, {"term": query, "first": limit}) or {}
		nodes = (data.get("searchIssues") or {}).get("nodes") or []
		return [
			Ticket(
				n["identifier"],
				"Linear",
				n.get("title") or "",
				n.get("url") or "",
				(n.get("state") or {}).get("name") or "",
				from_search=True,
			)
			for n in nodes
			if not self.teams or n["identifier"].split("-")[0] in self.teams
		]

	def _query(self, query: str, variables: dict[str, Any]) -> dict[str, Any] | None:
		response = httpx.post(
			LINEAR_API,
			json={"query": query, "variables": variables},
			headers={"Authorization": self.key},
			timeout=20,
		)
		if response.status_code != 200:
			return None
		return response.json().get("data")


CLIENTS: dict[str, Any] = {"Jira": JiraClient, "Linear": LinearClient}


def adf_text(node: Any) -> str:
	"""Plain text from Atlassian Document Format."""
	if node is None:
		return ""
	if isinstance(node, str):
		return node
	if isinstance(node, list):
		return "".join(adf_text(n) for n in node)
	text = node.get("text") or ""
	children = adf_text(node.get("content") or [])
	block = node.get("type") in ("paragraph", "heading", "listItem", "codeBlock", "blockquote")
	return text + children + ("\n" if block else "")
