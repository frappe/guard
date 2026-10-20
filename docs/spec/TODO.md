# TODO

Build order. Each milestone ends with passing tests and one commit or more. Feature ids refer to [01-features.md](01-features.md) and [13-ideas.md](13-ideas.md).

## M0. Spec

- [x] Feature inventory, architecture, data model, graph, review engine, config, conversations, learning, knowledge base, integrations, surfaces, security, ideas.

## M1. Foundations

- [ ] Python dependencies in `pyproject.toml`: `tree-sitter`, `tree-sitter-language-pack`, `pathspec`.
- [ ] Roles: Guard Admin, Guard Member, Guard Viewer.
- [ ] DocTypes from [03-data-model.md](03-data-model.md), with controllers and type hints.
- [ ] `after_install` and `after_migrate`: Organization Review Settings, the `guard_vector` table, the FULLTEXT index.
- [ ] Development site `guard.localhost` with Guard installed.

## M2. LLM layer

- [ ] `LLMClient` with chat, tool calls and JSON output.
- [ ] OpenAI dialect (Grove default) and Anthropic dialect with prompt caching (Q3).
- [ ] Offline fallback for tests.
- [ ] Embeddings: gateway model and hash fallback.
- [ ] Retry on 429 and 402, usage counting.
- [ ] Fetch models from `/v1/models`.

## M3. Providers and webhooks

- [ ] `ProviderAdapter` base and the GitHub adapter (App and token).
- [ ] Webhook endpoints with signature checks and Webhook Delivery.
- [ ] Event mapping, push or rebase detection.
- [ ] Installation sync, repository sync, auto-enable.
- [ ] Polling mode (O1).

## M4. Codebase graph

- [ ] Git store: mirror, fetch with header auth, ls-tree, show, diff, merge base, grep, log.
- [ ] Diff parser with line maps.
- [ ] Language specs and tree-sitter parser.
- [ ] Entities, raw edges, resolution, chunks.
- [ ] Frappe plugin (G1).
- [ ] Incremental index and full rebuild jobs.
- [ ] Vector store (MariaDB `VECTOR`, numpy fallback) and hybrid search.
- [ ] `CodeGraph` queries and the pull request overlay graph.

## M5. Config

- [ ] Schema and validation for `config.toml`, `guard.toml`, `files.toml`.
- [ ] Cascade resolver for one file and for the pull request, with explain.
- [ ] Filter globs and path globs.
- [ ] Review gate and tier selection.

## M6. Review engine

- [ ] Pipeline with stages, cancellation, retry and the credit cap.
- [ ] Context builder with budget.
- [ ] Agent tools and agent loop. Tier strategies.
- [ ] Verification, filter, dedupe, cap, score.
- [ ] Secret scan (Q2), Frappe review pack (Q1), blast radius (G2), test impact (G4).
- [ ] Renderer: summary comment, inline comments, footer, signed links.
- [ ] Publish: comments, status, reactions, description update.

## M7. Conversations

- [ ] Mention parsing and intents.
- [ ] Conversation agent and thread replies with classifier.
- [ ] Clarification follow-up.
- [ ] Tour generation.
- [ ] Chat with PR engine, chat actions and permission modes.
- [ ] Codebase chat engine.

## M8. Learning

- [ ] Reaction polling.
- [ ] Reply sentiment.
- [ ] Addressed detection on push and on close. Thread resolve.
- [ ] Memories with dedupe.
- [ ] Suppression and the adaptive noise filter.
- [ ] Suggested rules job. Rule metrics.
- [ ] Auto-approve and approval dismissal.

## M9. Knowledge and context

- [ ] Knowledge base areas, flows, build, sync, human edits.
- [ ] Jira, Confluence and Linear clients. Ticket detection.
- [ ] Cross-repo context and clusters. Suggested clusters.
- [ ] Project rule files.

## M10. Surfaces

- [ ] Query API with streaming.
- [ ] MCP server and tools.
- [ ] CLI with login, review, show, status, config, settings, fix bridge.
- [ ] Analytics API.

## M11. Desk UI

- [ ] Workspace, number cards, dashboard charts.
- [ ] Form scripts for Pull Request, Code Review, Review Finding, Code Repository, Custom Context, Code Provider, Guard Settings.
- [ ] Page `guard-pr`: list, overview, tour, diff, chat, shortcuts, impact diagram (G3).
- [ ] Page `guard-chat`.
- [ ] Reports: Guard Analytics, Bugs Caught, My Guard.

## M12. Runtime validation and admin

- [ ] Sandbox runner with podman or docker and its agent tools.
- [ ] Who-Guard-answers permissions.
- [ ] Usage cap. Feature tips. Retention jobs.

## M13. Finish

- [ ] Unit tests for every module. Pipeline test with a fake provider and the offline LLM.
- [ ] End-to-end test on a real repository with a local git server or a test GitHub repository.
- [ ] README with setup steps.
