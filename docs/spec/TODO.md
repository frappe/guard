# TODO

Build order. Each milestone ends with passing tests and one commit or more. Feature ids refer to [01-features.md](01-features.md) and [13-ideas.md](13-ideas.md).

## M0. Spec

- [x] Feature inventory, architecture, data model, graph, review engine, config, conversations, learning, knowledge base, integrations, surfaces, security, ideas.

## M1. Foundations

- [x] Python dependencies in `pyproject.toml`: `tree-sitter`, `tree-sitter-language-pack`, `pathspec`.
- [x] Roles: Guard Admin, Guard Member, Guard Viewer.
- [x] DocTypes from [03-data-model.md](03-data-model.md), with controllers and type hints.
- [x] `after_install` and `after_migrate`: Organization Review Settings, the `guard_vector` table, the FULLTEXT index.
- [x] Development site `guard.localhost` with Guard installed.

## M2. LLM layer

- [x] `LLMClient` with chat, tool calls and JSON output.
- [x] OpenAI dialect (Grove default) and Anthropic dialect with prompt caching (Q3).
- [x] Offline fallback for tests.
- [x] Embeddings: gateway model and hash fallback.
- [x] Retry on 429 and 402, usage counting.
- [x] Fetch models from `/v1/models`.

## M3. Providers and webhooks

- [x] `ProviderAdapter` base and the GitHub adapter (App and token).
- [x] Webhook endpoints with signature checks and Webhook Delivery.
- [x] Event mapping, push or rebase detection.
- [x] Installation sync, repository sync, auto-enable.
- [x] Polling mode (O1).

## M4. Codebase graph

- [x] Git store: mirror, fetch with header auth, ls-tree, show, diff, merge base, grep, log.
- [x] Diff parser with line maps.
- [x] Language specs and tree-sitter parser.
- [x] Entities, raw edges, resolution, chunks.
- [x] Frappe plugin (G1).
- [x] Incremental index and full rebuild jobs.
- [x] Vector store (MariaDB `VECTOR`, numpy fallback) and hybrid search.
- [x] `CodeGraph` queries and the pull request overlay graph.

## M5. Config

- [x] Schema and validation for `config.toml`, `guard.toml`, `files.toml`.
- [x] Cascade resolver for one file and for the pull request, with explain.
- [x] Filter globs and path globs.
- [x] Review gate and tier selection.

## M6. Review engine

- [x] Pipeline with stages, cancellation, retry and the credit cap.
- [x] Context builder with budget.
- [x] Agent tools and agent loop. Tier strategies.
- [x] Verification, filter, dedupe, cap, score.
- [x] Secret scan (Q2), Frappe review pack (Q1), blast radius (G2), test impact (G4).
- [x] Renderer: summary comment, inline comments, footer, signed links.
- [x] Publish: comments, status, reactions, description update.

## M7. Conversations

- [x] Mention parsing and intents.
- [x] Conversation agent and thread replies with classifier.
- [x] Clarification follow-up.
- [x] Tour generation.
- [x] Chat with PR engine, chat actions and permission modes.
- [x] Codebase chat engine.

## M8. Learning

- [x] Reaction polling.
- [x] Reply sentiment.
- [x] Addressed detection on push and on close. Thread resolve.
- [x] Memories with dedupe.
- [x] Suppression and the adaptive noise filter.
- [x] Suggested rules job. Rule metrics.
- [x] Auto-approve and approval dismissal.

## M9. Knowledge and context

- [x] Knowledge base areas, flows, build, sync, human edits.
- [x] Jira, Confluence and Linear clients. Ticket detection.
- [x] Cross-repo context and clusters. Suggested clusters.
- [x] Project rule files.

## M10. Surfaces

- [x] Query API with streaming.
- [x] MCP server and tools.
- [x] CLI with login, review, show, status, config, settings, fix bridge.
- [x] Analytics API.

## M11. Desk UI

- [x] Workspace, number cards, dashboard charts.
- [x] Form scripts for Pull Request, Code Review, Review Finding, Code Repository, Custom Context, Code Provider, Guard Settings.
- [x] Page `guard-pr`: list, overview, tour, diff, chat, shortcuts, impact diagram (G3).
- [x] Page `guard-chat`.
- [x] Reports: Guard Analytics, Bugs Caught, My Guard.

## M12. Runtime validation and admin

- [x] Sandbox runner with podman or docker and its agent tools.
- [x] Who-Guard-answers permissions.
- [x] Usage cap. Feature tips. Retention jobs.

## M13. Finish

- [x] Unit tests for every module. Pipeline test with a fake provider and the offline LLM.
- [x] End-to-end test on a real repository with a local git server or a test GitHub repository.
- [x] README with setup steps.

## Later

Ideas marked "later" in [13-ideas.md](13-ideas.md): G5, G6, Q4, Q5, Q6, O2, O3, P1 to P4.
