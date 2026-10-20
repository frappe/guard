# Guard: Overview

Guard is a Frappe app that reviews pull requests with an AI agent. The agent reads the full codebase, not only the diff. Guard builds a graph of every repository it reviews and uses that graph to find the callers, the callees and the similar code of each change.

This document set is the specification. Code follows the spec. When the code and the spec do not agree, fix one of them in the same change.

## Document map

| File | Subject |
| - | - |
| [00-overview.md](00-overview.md) | Purpose, users, glossary, principles, scope |
| [01-features.md](01-features.md) | Feature inventory with acceptance criteria |
| [02-architecture.md](02-architecture.md) | Components, processes, data flow |
| [03-data-model.md](03-data-model.md) | DocTypes and tables |
| [04-codebase-graph.md](04-codebase-graph.md) | Indexing, parsing, graph storage, graph queries |
| [05-review-engine.md](05-review-engine.md) | Review pipeline, tiers, agent, findings, output |
| [06-configuration.md](06-configuration.md) | Dashboard settings, `.guard/` cascade, `guard.json`, filters |
| [07-conversations.md](07-conversations.md) | Mentions, thread replies, Chat with PR, actions, tour |
| [08-learning.md](08-learning.md) | Feedback, addressed detection, memories, suppression |
| [09-knowledge-base.md](09-knowledge-base.md) | Generated repository documentation |
| [10-integrations.md](10-integrations.md) | Code providers, Jira, Linear, cross-repo context |
| [11-surfaces.md](11-surfaces.md) | Desk UI, query API, MCP server, CLI, fix bridge |
| [12-security.md](12-security.md) | Roles, permissions, secrets, untrusted input |
| [13-ideas.md](13-ideas.md) | Brainstorm: ideas past the baseline |
| [TODO.md](TODO.md) | Build order and task list |

## Users

| User | Goal |
| - | - |
| Developer | Gets a fast first review on each pull request. Asks follow-up questions. Fixes findings with a coding agent. |
| Reviewer | Reads a summary, a risk score and a diagram before reading the diff. |
| Team lead | Sets standards, rules and strictness. Reads analytics. |
| Admin | Connects code providers, picks LLM models, manages people and repositories. |
| Coding agent | Reads findings, rules and the knowledge base through MCP or the API. |

## Glossary

Use these names. Do not use other names for the same thing.

| Term | Meaning |
| - | - |
| Code provider | A connected source code host: GitHub, GitLab, Gitea or Bitbucket. |
| Team | One provider namespace: a GitHub organization, a GitLab group, a Gitea organization or a Bitbucket workspace. |
| Repository | One repository on a code provider. |
| Index | The graph, the chunks and the vectors of one repository at one branch. |
| Entity | A node in the graph: a file, a class, a function, a method, a variable or a type. |
| Edge | A directed link between two entities: contains, imports, calls, inherits or references. |
| Chunk | A slice of source text that the search uses. A chunk usually maps to one entity. |
| Pull request | A pull request or a merge request. Guard uses "pull request" for both. |
| Review | One run of the review engine on one pull request (or one branch) at one commit. |
| Finding | One issue that a review reports. A finding becomes an inline comment. |
| Summary comment | The top-level comment of a review. |
| Severity | P0 (critical), P1 (high) or P2 (medium). |
| Comment type | logic, syntax or style. |
| Strictness | 1 (verbose), 2 (balanced) or 3 (critical only). |
| Tier | The review depth: base, plus, apex or auto. |
| Confidence score | A 0 to 5 score that tells if the pull request is ready to merge. |
| Risk level | low, medium, high or critical. Auto-approve uses it. |
| Custom context | A rule, a file reference, an instruction or a learned pattern that the reviewer reads. |
| Memory | Custom context that Guard learned from feedback. |
| Knowledge base | Generated documents that explain one repository. |
| Repository cluster | A group of repositories that share context during reviews. |
| Trigger handle | The `@` name that starts Guard from a comment. The default is `@guard`. |
| Credit | The unit of review cost. Base costs 1, plus costs 3, apex costs 10. |

## Principles

1. **Context over diff.** Every review queries the graph. A review that reads only the diff is a defect.
2. **Signal over volume.** Post fewer, better comments. Suppress what the team ignores. Never suppress security or data-loss findings.
3. **Config as code.** Settings live in the repository when possible. The closest config wins. Rules add up.
4. **Provider-neutral core.** The review engine does not know which provider it talks to. Adapters translate.
5. **Model-neutral core.** The engine talks to one LLM interface. Admins pick the provider and the models.
6. **Works offline for tests.** Every LLM and embedding call has a deterministic fallback, so tests run with no network.
7. **Untrusted input.** Code, comments, tickets and knowledge base text are data, not instructions.
8. **Frappe native.** DocTypes, roles, background jobs, realtime and the REST API come from Frappe. Do not rebuild them.

## Scope

In scope: everything in [01-features.md](01-features.md).

Out of scope:

- Billing and payment. Guard counts credits and enforces a usage cap. It does not charge money.
- SAML and directory sync. Frappe login, OAuth and LDAP cover sign-in.
- Perforce and other hosts that are not listed in [10-integrations.md](10-integrations.md).
- Hosted multi-tenant operation. One Frappe site is one organization: one company.
- A separate front end. Employees use Frappe Desk. Each employee sees their own stats there.
