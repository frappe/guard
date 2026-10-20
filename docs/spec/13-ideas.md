# Ideas

Ideas past the baseline in [01-features.md](01-features.md). Each idea has a status:

- **v1**: build with the baseline.
- **later**: keep for a later release.

## Graph

| # | Idea | Status |
| - | - | - |
| G1 | **Frappe-aware graph.** DocType JSON files become `doctype` entities with fields. `Link` and `Table` fields become edges. `hooks.py` dotted paths (doc events, scheduler events, overrides) become `references` edges. `frappe.call`, `frappe.xcall` and `frm.call` in JavaScript link to the whitelisted Python method, which gives cross-language edges. `@frappe.whitelist()` functions are API entry points, with the `allow_guest` flag. | v1 |
| G2 | **Blast radius line** in the summary: "Affects 14 callers in 6 files. 3 callers are not updated." Built from the graph, no LLM. | v1 |
| G3 | **Impact diagram** for a pull request: a Mermaid graph of changed entities and their callers, built from the graph. Shown on the Overview tab. | v1 |
| G4 | **Test impact.** Map changed entities to the tests that call them. List the tests to run. Flag a public function with no test path. | v1 |
| G5 | Cross-language HTTP edges: route definitions to client `fetch` calls. | later |
| G6 | Ownership from `CODEOWNERS` and `git blame`. Suggest reviewers. | later |

## Review quality

| # | Idea | Status |
| - | - | - |
| Q1 | **Frappe review pack.** Built-in rules for Frappe code, on when the repository is a Frappe app: permission checks in whitelisted methods, `allow_guest` endpoints, `ignore_permissions=True`, string-formatted `frappe.db.sql`, `frappe.get_doc` in loops, schema changes with no patch, user-facing strings without `_()`. | v1 |
| Q2 | **Secret scan** of the diff before the LLM sees it: known token shapes and high-entropy strings. A hit is a P0 finding. | v1 |
| Q3 | **Prompt caching** through the Anthropic dialect `cache_control` on the stable part of the context. Grove prices cached tokens lower. | v1 |
| Q4 | Two-model consensus for apex: two models review apart, Guard keeps findings that both report or that have high confidence. | later |
| Q5 | Dependency check: query OSV for new or bumped packages. | later |
| Q6 | Conflict warning: other open pull requests that change the same entities. | later |

## Operations

| # | Idea | Status |
| - | - | - |
| O1 | **Polling mode** for providers that cannot reach the site with webhooks, for example a laptop bench. A job lists open pull requests every 5 minutes and makes the same events. | v1 |
| O2 | **Replay and eval.** Run a past pull request again with new settings or another model, and compare. Up-voted and addressed findings are the positive set. | later |
| O3 | Cost view: tokens and gateway spend for each review, team and month, from gateway usage. | later |
| O4 | Pre-push hook recipe with `guard review status`. | v1 (docs only) |

## Platform

| # | Idea | Status |
| - | - | - |
| F1 | **Flow integration.** When the Flow app is installed, Guard registers its graph and knowledge tools (`search_code`, `find_definition`, `get_callers`, `get_callees`, `read_file`, `search_knowledge_base`, `list_findings`) through the `flow_tools` hook, and a "Guard" agent through `flow_agents`. Flow sessions then give codebase chat with transcripts and approvals. The review pipeline does not run on Flow: it needs a bounded synchronous loop inside an RQ job, a forced final tool call, and offline tests. | v1 |

## People

| # | Idea | Status |
| - | - | - |
| P1 | Raven or Gameplan post with the review summary for watched repositories. | later |
| P2 | Weekly digest for each developer: open findings, addressed rate, recurring issues. | later |
| P3 | Rule dry run: test a rule on a past pull request before it goes active. | later |
| P4 | Apply all suggestions as one commit, after approval. | later |
