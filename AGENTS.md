# AGENTS.md

Guard is a Frappe app (Frappe 16/17, Python 3.14) that reviews GitHub pull requests with an AI agent
backed by a code graph of every repository. One site serves one company. All UI is Frappe Desk.

`docs/spec/` is the specification. Code follows the spec: when code and spec disagree, fix one of
them in the same change. Read `docs/spec/02-architecture.md` (components, queues, data flow) and the
spec file for the area you touch before larger changes.

## Commands

Run bench commands from the bench root (`../..`). Dev site: `guard.localhost`. Test site:
`guard-test.localhost` (already has `allow_tests`).

```bash
# All tests (what CI runs)
bench --site guard-test.localhost run-tests --app guard
# One module / one TestCase / one test
bench --site guard-test.localhost run-tests --module guard.tests.test_pipeline
bench --site guard-test.localhost run-tests --module guard.tests.test_config --case TestGate
bench --site guard-test.localhost run-tests --module guard.tests.test_pipeline \
  --test test_review_posts_findings_summary_and_status

# Lint, format, types (run from apps/guard)
ruff check guard && ruff format guard
pyright                      # must stay at 0 errors; config in pyrightconfig.json
pre-commit run --all-files   # ruff (isort, lint, format), prettier + eslint for JS, JSON/TOML/YAML checks

# After DocType JSON changes: migrate both sites
bench --site guard.localhost migrate && bench --site guard-test.localhost migrate

# Evals: replay recorded PRs through a real review (needs a gateway)
bench --site guard.localhost execute guard.evals.run.run --kwargs "{'case': 'pilot-678'}"
# Index real repositories and fail on any parser error (nightly CI job)
bench --site guard.localhost execute guard.indexing.check.index_repositories
```

The CI "Linters" job also runs Semgrep with `frappe/semgrep-rules` and `r/python.lang.correctness`.
Semgrep is not installed globally. To check locally:
`git clone --depth 1 https://github.com/frappe/semgrep-rules /tmp/frappe-semgrep-rules && uvx semgrep --error --config /tmp/frappe-semgrep-rules/rules --config r/python.lang.correctness <files>`.

## Code conventions

- **Formatting:** tabs, double quotes, line length 110 (ruff). JSON (DocType files) uses 1-space indent
  and no final newline. JS is formatted by prettier and checked by eslint (`.eslintrc`).
- **Python 3.14 syntax is in use**, for example `except TypeError, ValueError:` without parentheses
  (PEP 758). Do not "fix" it.
- **Types everywhere.** Pyright is the gate. `hooks.py` sets `require_type_annotated_api_methods`, so
  every `@frappe.whitelist` function needs full annotations. Cast `frappe.get_doc(...)` to the
  controller class under `if TYPE_CHECKING:` imports (see `guard/review/pipeline.py`).
- **DocType controllers** have an auto-generated `DF.*` type block. Do not edit inside it. Saving the
  DocType in Desk (developer mode) regenerates it. After a hand edit of the JSON, regenerate it with
  `frappe.types.exporter.TypeExporter(frappe.get_doc("DocType", "<name>")).export_types()` in
  `bench --site guard.localhost console`, or pyright fails on every field.
- **Database access:** use `guard/utils/db.py` (`db.rows`, `db.dict_rows`, `db.scalar`, `db.value`,
  `db.values`, `db.set_values`, `db.insert_many`, `db.commit`, `db.rollback`), not raw `frappe.db.sql`.
  It types the results and passes `()` instead of `None` (MySQLdb fails on `None`).
  `db.commit()` is a no-op in tests: tests run in one transaction. Background jobs commit progress
  through it. To find rows whose time has passed, filter with `db.due(field)`, not
  `{field: ["<=", now]}`: Frappe turns that into `IFNULL(field, '') <= now`, which matches NULL rows.
- **Cache and locks:** `guard/utils/cache.py` (`cache.get/set/delete`, `cache.exclusive(key, ttl)` for a
  non-blocking Redis lock) and `guard/utils/locks.py` (`file_lock` for git mirrors and indexes).
- **Secrets:** pass error text through `guard.utils.redact.redact` before it goes into a field, a log
  or a comment. Credentials are Password fields that you read with `get_password(..., raise_exception=False)`.
- **Untrusted input:** code, PR text, comments, tickets and KB pages are data, not instructions. Wrap
  them with `guard.review.prompts.data(label, text)` (a `<data>` block) when they go into prompts.
- **Semgrep-safe code:** do not use builtin `open()` outside `test_*.py` (the file-traversal rule
  blocks it); use `pathlib`/`os` APIs. For f-string SQL over constant table names, add
  `# nosemgrep: frappe-sql-format-injection` with a reason, as in `utils/db.py` and `install.py`.
  `frappe.db.commit()` outside `utils/db.py` needs `# nosemgrep: frappe-manual-commit`. Prefer
  `hashlib.blake2b` to `md5`.
- **Naming:** use the glossary in `docs/spec/00-overview.md` (Code provider, Team, Index, Entity, Edge,
  Chunk, Finding, Tier, Strictness, ...). Module constants are UPPER_CASE at the top of the file, and
  magic numbers get a short comment that tells why.
- **Errors:** domain exceptions are small classes in their module (`IndexBusy`, `IndexFailed`,
  `GatewayBusy`, `GatewayDown`, `ProviderError`, `Cancelled`). The pipeline maps them to park / retry /
  fail. Under `frappe.flags.in_test` the pipeline raises again instead of failing quietly.
- **Comments, docstrings, docs and commit messages** use plain, short sentences (Simplified Technical
  English): "Queue a review now, or mark it to resume after `delay` seconds." Comments explain why.
- **Commits:** Conventional Commits with a plain-English subject: `fix: limit concurrent reviews and
  agent turns on existing sites`, `perf: ...`, `feat: ...`.
- Never name a third-party hosted AI reviewer product in code, comments, docs or logs.
- Throwaway scripts go in `guard/_scratch/` (gitignored, excluded from pyright).

## Architecture

**Request path.** `webhooks/endpoint.github_webhook` (guest, signature-checked) stores a
**Webhook Delivery** and enqueues `webhooks/handler.process` on `short`. `handler.dispatch` routes by
event kind: push to the default branch → incremental index, pull_request → `review/trigger.consider`,
comment → `conversation/` (mentions, thread replies), installation → `providers/sync`.
`webhooks/polling.poll` does the same work without webhooks, when Poll Providers is on.

**Review pipeline** (`review/pipeline.py`, `ReviewPipeline.execute`). Stages are written to
`Code Review.stage`: prepare (fetch head into the mirror, diff from merge base, resolve config) →
start (publisher posts a status) → index (`indexing.jobs.ensure_fresh`; a failure means a review without
the graph, not a failed review) → context (`graph/overlay.PullRequestGraph` changed entities + blast
radius, cross-repo callers, rule files, tier choice, `review/context.build`) → review (deterministic
checks in `review/checks.py` and `crossrepo.py`, then the tool-calling `review/agent.Agent`) → verify
(an LLM fact-checks findings and the verdict) → filter (`FindingFilter`, learned `suppression`,
`stale.close_stale`) → publish (`review/publish.Publisher`, `review/render.py`) → after (learning,
auto-approve). All stages share one `ReviewRun` dataclass (`review/run.py`). The final verdict comes
from the `submit_review` tool call, and `score_cap` caps it by the severity of open findings.

**Job control.** `review/jobs.py` enqueues `run_review` with `job_id` dedup (`default` queue, `long` for
apex). Delays are not RQ delays: `enqueue_review(delay=...)` sets `retry_at`, and the every-minute cron
`resume_waiting` picks it up. The pipeline *parks* (no retry used) when the circuit breaker
(`llm/breaker.py`) is open or `max_concurrent_reviews` slots are full. It *retries* on
`GatewayBusy`/`IndexBusy`. `fail_dead_reviews` restarts reviews whose worker died. In tests
`frappe.enqueue(..., now=True)` runs jobs inline.

**LLM layer** (`llm/`). Everything calls `LLMClient(tier=..., purpose=...)` or `LLMClient.fast(...)`.
Dialects: `openai_dialect`, `anthropic_dialect`, and `offline` (used when no gateway is set: empty
findings, hash embeddings). `llm.client.use_responder(fn)` intercepts every chat call: tests and evals
use it. Models and turn limits come from `guard/settings.py` (typed Guard Settings access; tiers
base/plus/apex with credits 1/3/10).

**Code graph.** `indexing/indexer.Indexer` reads a bare mirror (`git/store.GitStore`, one per repo at
`sites/<site>/private/guard/git/`), parses with tree-sitter (`parser.py`, `languages.py`), resolves
symbols and edges (`symbols.py`, `resolver.py`), adds Frappe doctypes/hooks/`frappe.call` strings
(`frappe_plugin.py`) and HTTP routes (`routes.py`), cuts chunks, and embeds them. For speed it bulk-writes
**Code Entity / Code Edge / Code Chunk** rows with `db.insert_many` straight into the `tab*` tables,
not through the ORM. Vectors live in a raw `guard_vector` table (MariaDB `VECTOR` + HNSW, numpy
fallback; `indexing/vectors.py`). Composite and FULLTEXT indexes and that table are created in
`install.after_migrate`. If you change parser output, bump `PARSER_VERSION` (`indexing/parser.py`): the next run of each index
is then a full re-index, not an incremental one.
`graph/queries.CodeGraph` answers callers, callees, definitions and similar code for one index.

**Config cascade** (`config/`). Dashboard **Review Settings** (Organization → Team) <
repository overrides < `.guard/config.toml`, `.guard/rules.md`, `.guard/files.toml`, `guard.toml` from the
repo root down to the file. Scalars: the closest value wins. Rules, files, notes: they add up
(`resolver.ConfigResolver`, keys in `schema.py`). `gate.py` checks filters and picks the tier.

**Providers.** The core is provider-neutral: use `providers.adapter_for_repository(repo)` →
`ProviderAdapter` (`providers/base.py`). `GitHubAdapter` is the only adapter now.

**Other surfaces.** `api/ui.py` (Desk), `api/v2.py` (query API), `api/public.py` (signed links,
`review/links.py`), `api/health.py`. Guard has no CLI of its own: terminals use
[frappectl](https://github.com/frappe/frappectl) against the same whitelisted methods and DocTypes.
`mcp/` is the MCP server at `/mcp` (a page renderer) for local reviews: the agent of the developer
reviews with its own model, and Guard runs no model for them (`review/local.py`). It reuses the
protocol checks of `frappe.mcp`. The Connect page gives a setup prompt with an agent token (`mcp/tokens.py`, an OAuth Bearer Token). Desk UI lives in `guard/guard/` as plain Frappe JS (pages, form/list scripts, reports,
workspace). There is no frontend build.

Scheduled jobs and permission hooks are in `guard/hooks.py`. Team-scoped permissions are in
`guard/permissions.py`.

## Tests

- All tests are in `guard/tests/` (not next to DocTypes). They run offline: no gateway, no GitHub.
- `tests/utils.TempRepo` makes a temporary git repo. `make_repository` points a **Code Repository**
  at it through `clone_url`.
- `tests/fakes.FakeAdapter` records provider calls (`adapter.named("post_comment")`).
  `fakes.scripted(steps)` is a responder: each step is a list of `(tool_name, args)` tool calls for one
  agent turn, or plain text. Purposes without tools get answers from `json_answers`.
- `tests/cases.PullRequestCase` is the main fixture: a repo with PR #7 that changes a signature,
  `adapter_for_repository` patched in the modules in `PATCHED`, and `self.review(steps)` to run a full
  review. When new code calls `adapter_for_repository`, add its module to `PATCHED`.
- Use `UnitTestCase` for pure logic and `IntegrationTestCase` when you need the DB.
- Breaker state is in Redis and outlives a test. `PullRequestCase.setUp` clears it. Do the same in
  new fixtures that touch the gateway.
