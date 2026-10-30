# Data model

All DocTypes are in the module "Guard". Field lists show the main fields only. Standard fields (`name`, `owner`, `creation`, `modified`) are not listed.

## Settings

### Guard Settings (single)

| Field | Type | Notes |
| - | - | - |
| `offline_mode` | Check | Make no calls to AI providers. Reviews post only the checks that need no model. A change rebuilds all vectors. |
| `default_ai_provider` | Link AI Provider | Serves every model whose AI provider field is blank. Blank means offline mode. |
| `request_timeout` | Int | Seconds for one request to an AI provider. Default 300. |
| `base_model`, `plus_model`, `apex_model` | Data | Model id for each tier, as its AI provider names it. |
| `fast_model` | Data | Classifiers, titles, small summaries. Blank uses the base tier. |
| `embedding_model` | Data | Blank means hash embeddings. |
| `base_ai_provider`, `plus_ai_provider`, `apex_ai_provider`, `fast_ai_provider`, `embedding_ai_provider` | Link AI Provider | The AI provider of each model. Blank uses `default_ai_provider`. |
| `embedding_dimensions` | Int | Size of the vector column. Changing it rebuilds vectors. |
| `max_turns_base`, `max_turns_plus`, `max_turns_apex` | Int | Agent turn limits. Defaults 10, 16, 24. |
| `<tier>_input_price`, `<tier>_cached_input_price`, `<tier>_output_price` | Float | For `base`, `plus` and `apex`: USD for one million tokens of the model of the tier. Guard Usage and Guard Health use them to estimate the cost of each review by its tier. The input price is for prompt tokens that the cache did not cover. A tier without prices uses the prices of the next lower tier, as it does for its model. 0 leaves that part out. |
| `trigger_handles` | Small Text | One handle each line. Default `@guard`. |
| `bot_logins` | Small Text | More provider logins that are Guard itself. Guard ignores its own comments. Guard also knows `<app_slug>[bot]` and the `bot_username` of each enabled Code Provider. |
| `public_url` | Data | Base URL for links in comments. Default: the site URL. |
| `trigger_by_authoring`, `trigger_by_tagging`, `chat_permission`, `teach_permission` | Select | `Everyone`, `Organization members`, `Guard users` |
| `monthly_credit_cap` | Int | 0 means no cap. |
| `outage_fallback_after` | Int | Seconds that a review waits for an AI provider that is down. Then it posts the checks that need no model, and a full review follows when the AI provider answers. Default 900. 0: reviews wait. |
| `push_review_delay` | Int | Seconds that an automatic review of a push waits. Default 120. 0 turns the wait off. |
| `pull_request_daily_tokens` | Int | Input tokens that the reviews of one pull request can use in 24 hours before a push gets no automatic review. Default 10,000,000. 0 means no limit. |
| `auto_enable_new_repositories` | Check | |
| `feature_tips` | Check | |
| `learn_from_activity` | Check | Default on. |
| `default_coding_agents` | Small Text | |
| `sandbox_enabled`, `sandbox_when`, `sandbox_runtime`, `sandbox_image`, `sandbox_setup_command`, `sandbox_timeout`, `sandbox_network` | mixed | Runtime validation. |

### AI Provider

An LLM API that Guard calls ([02-architecture.md](02-architecture.md#ai-providers)). Named by `provider_name`. Guard Admin and System Manager only.

| Field | Type | Notes |
| - | - | - |
| `provider_name` | Data | For example `Grove`, `OpenRouter`, `Anthropic`. |
| `enabled` | Check | A disabled AI provider serves no model: its models are offline. |
| `dialect` | Select | `openai`, `anthropic` |
| `url` | Data | Base URL without `/v1`. Guard removes a trailing `/v1`. |
| `api_key` | Password | |

The patch `move_gateway_to_ai_provider` makes the gateway of a site from before (`gateway_url`, `gateway_api_key`, `gateway_dialect`) the AI provider `Gateway`, and the default.

### Review Settings

One record for the organization (name `Organization`) and one for each team that overrides.

| Field | Type | Notes |
| - | - | - |
| `scope` | Select | `Organization`, `Team` |
| `team` | Link Guard Team | |
| `inherit_from_organization` | Check | Team record only. |
| `strictness` | Select | `1`, `2`, `3` |
| `comment_logic`, `comment_syntax`, `comment_style` | Check | |
| `auto_review` | Select | `Never`, `On pull request opened`, `On new pushes`, `On all events` |
| `review_drafts` | Check | |
| `review_tier` | Select | `base`, `plus`, `apex`, `auto` |
| `filter_rules` | Table Review Filter Rule | |
| `ignore_patterns` | Code | `.gitignore` syntax |
| `instructions` | Long Text | |
| `exclude_authors`, `disabled_labels`, `exclude_branches`, `ignore_keywords`, `include_authors`, `labels`, `include_branches` | Small Text | One value on each line. Same meaning as the config keys. |
| `<section>_included`, `<section>_collapsible`, `<section>_open` | Check | For `summary`, `issues_table`, `confidence`, `impact`, `diagram`. |
| `comments_outside_diff` | Check | |
| `status_check`, `status_comments`, `update_description`, `update_summary_only`, `fix_with_ai`, `hide_footer` | Check | |
| `auto_approve`, `risk_ceiling`, `risk_definitions`, `auto_approve_filters` | mixed | `auto_approve_filters` is JSON with the keys of `[auto_approve.filters]`. |
| `include_rule_files` | Check | Read `CLAUDE.md` and similar files. |

### Review Filter Rule (child)

| Field | Type | Notes |
| - | - | - |
| `label` | Data | |
| `enabled` | Check | |
| `conditions` | JSON | `[{"field": "author", "operator": "exclude", "values": ["dependabot[bot]"]}]` |
| `review_tier` | Select | Blank, `base`, `plus`, `apex`, `auto` |

Fields: `author`, `target_branch`, `source_branch`, `label`, `title`, `keyword`, `file_path`, `files_changed`, `repository`. Operators: `include`, `exclude`, `more_than`, `less_than`.

## Organization

### Code Provider

`title`, `provider_type` (GitHub), `api_url`, `web_url`, `auth_mode` (App, Token), `app_id`, `app_slug`, `private_key` (Password), `token` (Password), `webhook_secret` (Password), `bot_username`, `enabled`, `verify_ssl`.

### Guard Team

`team_name`, `code_provider`, `namespace` (for example `acme`), `provider_namespace_id`, `installation_id` (GitHub App), `avatar_url`, `members` (Table Guard Team Member: `user`, `role` = Admin or Member).

### Guard User

One record for each Frappe user. Name = user id.

`user`, `github_login`, `github_token` (Password), `coding_agents`, `show_summary`, `show_diagram`, `collapsible_sections`, `bridge_seen_at`.

### Code Repository

| Field | Type | Notes |
| - | - | - |
| `full_name` | Data | `owner/repo`. Unique with the provider. |
| `code_provider`, `team` | Link | |
| `provider_repo_id` | Data | |
| `default_branch`, `clone_url`, `web_url` | Data | |
| `is_private` | Check | |
| `reviews_enabled` | Check | |
| `review_tier`, `strictness` | Select | Optional overrides. |
| `index_status` | Select | `Not Indexed`, `Queued`, `Indexing`, `Indexed`, `Failed` |
| `default_index` | Link Repository Index | |
| `languages` | JSON | Bytes for each language. |
| `kb_status` | Select | `None`, `Queued`, `Building`, `Ready`, `Failed`. Queued from the request that asks for a build until its job starts. |

The record name is a random hash. MCP and the API call it the namespace id.

### Repository Index

`repository`, `branch`, `status` (`submitted`, `cloning`, `processing`, `completed`, `failed`), `sha`, `files_processed`, `num_files`, `entity_count`, `edge_count`, `chunk_count`, `error`, `started_at`, `completed_at`.

### Repository Cluster

`cluster_name`, `status` (Active, Suggested, Discarded), `confidence`, `members` (Table Repository Cluster Member: `repository`).

## Graph

The indexer writes these with bulk inserts. Names are content hashes, so a re-index of an unchanged file writes nothing.

### Code Entity

`index`, `repository`, `path`, `kind` (file, module, class, function, method, variable, type, interface, doctype, field), `entity_name`, `qualified_name`, `parent_entity`, `language`, `start_line`, `end_line`, `signature`, `docstring`, `is_exported`, `content_hash`.

### Code Edge

`index`, `repository`, `source_entity`, `target_entity` (blank when not resolved), `target_name`, `kind` (contains, imports, calls, inherits, references), `path`, `line`.

### Code Chunk

`index`, `repository`, `path`, `entity`, `start_line`, `end_line`, `language`, `content` (Long Text, FULLTEXT index), `content_hash`.

### `guard_vector` (plain table, not a DocType)

| Column | Type |
| - | - |
| `name` | varchar(140), primary key |
| `kind` | varchar(20): `chunk`, `finding`, `context`, `kb` |
| `ref` | varchar(140): the record name |
| `repository` | varchar(140) |
| `embedding` | `VECTOR(n)` with a cosine HNSW index, or `LONGBLOB` in fallback mode |

## Reviews

### Pull Request

`repository`, `number`, `title`, `description`, `author`, `state` (open, closed, merged), `is_draft`, `base_branch`, `head_branch`, `base_sha`, `head_sha`, `first_commit_sha`, `head_clone_url`, `web_url`, `labels` (JSON), `opened_at`, `merged_at`, `closed_at`, `additions`, `deletions`, `changed_files`, `review_count`, `last_reviewed_sha`, `summary_comment_id`, `latest_review`, `backport_of` (the numbers of the pull requests that it backports, for example `#7, #9`), `confidence_score`, `risk_level`, `guard_approved`, `approval_id`, `tour` (JSON), `tour_sha`, `provider_node_id`, `addressed_attempt`, `addressed_retry_at`, `addressed_since`, `retriggered_at`.

`retriggered_at` is when a signed re-trigger link last started a review ([12-security.md](12-security.md)).

`addressed_attempt` and `addressed_retry_at` hold the next try of an addressed check that met a busy or failing gateway. `addressed_since` is the `before` commit of the first push that was not checked.

A repository has one row for each pull request number: a unique index on `repository, number` makes sure. Events of one pull request often arrive in the same second, each in its own job. `pull_requests.upsert` locks an existing row, so these jobs write one after the other. The second job to insert a new row meets the unique index and updates the row of the first one. The lock lasts until the transaction ends, so a caller commits before slow work.

### Code Review

| Field | Type | Notes |
| - | - | - |
| `pull_request`, `repository` | Link | `pull_request` is blank for CLI reviews. |
| `source` | Select | `pull_request`, `cli`, `api` |
| `status` | Select | `Queued`, `Running`, `Completed`, `Failed`, `Cancelled`, `Skipped` |
| `trigger` | Select | `open`, `push`, `rebase`, `mention`, `retrigger`, `manual`, `cli`, `api` |
| `requested_by` | Data | Provider login. |
| `requested_tier`, `tier` | Select | |
| `head_sha`, `base_sha`, `merge_base_sha`, `branch` | Data | |
| `instructions` | Long Text | From the trigger comment or the CLI. |
| `strictness` | Int | |
| `summary` | Markdown | |
| `confidence_score`, `confidence_reason` | Int, Small Text | |
| `risk_level`, `risk_reason` | Select, Small Text | |
| `diagram_type`, `diagram` | Select, Code | |
| `important_files`, `context_used`, `changed_files`, `config_snapshot` | JSON | |
| `files_reviewed`, `findings_count`, `posted_count`, `suppressed_count` | Int | |
| `credits`, `input_tokens`, `output_tokens` | Int | `input_tokens` is the whole prompt of every call. Token and turn counts add up over all attempts, also the ones that failed, were cancelled or were killed. Guard writes them before each agent turn. |
| `outage_fallback` | Check | No model read this review: an AI provider was down. A full review of the same commit follows. |
| `cached_tokens` | Int | The part of `input_tokens` that the gateway read from its prompt cache |
| `agent_turns` | Int | Model turns of the review agents |
| `agent_round_limit` | Int | Rounds that the agents of the review can take: the turn limit, lower on a re-review of a push or a backport, or both passes of an apex review in parts. A push lets a review finish after half of them. |
| `usage_detail` | JSON | `reasoning_tokens`; `purposes`: for each purpose of a call (`review`, `verify`, `verdict`, `stale`, ...), its `calls`, `input`, `cached`, `output` and `reasoning` tokens; `tools`: for each agent tool, its `calls` and the `chars` of output. Adds up over all attempts, as the token counts do. |
| `agent_rounds` | Int | Rounds of the review agents. In a round, each busy agent takes one turn, so an apex review has fewer rounds than turns. |
| `review_number` | Int | 1 for the first review of a pull request. |
| `started_at`, `completed_at`, `error`, `body` | mixed | `body` is the rendered summary comment. |
| `duration`, `stage_seconds` | Float, JSON | Seconds of the last attempt, and the seconds in each stage (`prepare` to `after`) over all attempts. |
| `stage`, `cancel_requested`, `attempt`, `retry_at` | mixed | Progress, cancellation, and the retry time after a busy gateway. A job every minute queues reviews whose `retry_at` has passed. |
| `cancelled_by` | Link User | The user who cancelled the review. Blank when a newer review or a closed pull request cancelled it. |
| `check_run_id`, `eyes_reaction` | Data | The provider ids of the Guard check on the head and of the 👀 on the pull request ([05-review-engine.md](05-review-engine.md#status)). |

### Review Finding

| Field | Type | Notes |
| - | - | - |
| `code_review`, `pull_request`, `repository` | Link | |
| `path`, `start_line`, `end_line`, `side` | mixed | `side` is `RIGHT` or `LEFT`. |
| `severity` | Select | `P0`, `P1`, `P2` |
| `comment_type` | Select | `logic`, `syntax`, `style` |
| `category` | Data | security, performance, correctness, concurrency, data, api, maintainability |
| `title`, `body`, `suggestion`, `fix_prompt`, `question` | text | |
| `confidence` | Float | 0 to 1 |
| `status` | Select | `Open`, `Addressed`, `Ignored`, `Dismissed`, `Suppressed`, `Outdated` |
| `status_reason`, `addressed_at`, `addressed_in_sha` | mixed | |
| `fingerprint` | Data | Hash of path, normalized title and anchor code. |
| `provider_comment_id`, `provider_thread_id`, `comment_url` | Data | |
| `upvotes`, `downvotes` | Int | |
| `custom_context` | Link | The rule that produced the finding. |
| `outside_diff`, `posted`, `verified` | Check | |
| `sandbox_log` | Long Text | |

### Pull Request Comment

Every comment on a pull request, human or bot. Learning, thread replies and comment search use it.

`pull_request`, `repository`, `provider_comment_id`, `kind` (issue, review, inline), `thread_id`, `in_reply_to`, `author`, `is_bot`, `is_guard`, `body`, `path`, `line`, `commented_at`, `finding`, `processed`, `waits_for_review`, `eyes_reaction`, `attempt`, `retry_at`.

`attempt` and `retry_at` hold the next try of a comment that met a busy or failing gateway. `waits_for_review` marks a mention that asked for a review, and `eyes_reaction` holds the id of its 👀 ([07-conversations.md](07-conversations.md)).

### Review Feedback

`finding`, `pull_request`, `repository`, `author`, `kind` (Upvote, Downvote, Reply, Addressed, Ignored, Missed), `sentiment` (Agree, Disagree, Context, Question, Neutral), `body`, `processed`.

### Sandbox Run

`code_review`, `finding`, `command`, `exit_code`, `output`, `duration`, `status`.

## Memory

### Custom Context

| Field | Type | Notes |
| - | - | - |
| `context_type` | Select | `Rule`, `File`, `Instruction`, `Other`, `Pattern` |
| `title`, `body` | text | |
| `file_path` | Data | For `File`. |
| `status` | Select | `Active`, `Inactive`, `Suggested`, `Ignored` |
| `status_reason` | Small Text | Who changed the status, or why Guard retired it. |
| `active_since` | Datetime | Set when the status becomes Active. Automatic retirement counts findings after it. |
| `severity` | Select | `low`, `medium`, `high` |
| `team` | Link | Blank means organization scope. |
| `repository_scope` | Small Text | One pattern each line, wildcards allowed (`acme/*`). Blank means all. |
| `path_scope` | Small Text | One glob each line. Blank means all. |
| `source` | Select | `Dashboard`, `Learned`, `Suggested`, `MCP`, `Chat` |
| `usage_count`, `addressed_count`, `merged_count` | Int | |
| `addressed_rate` | Percent | `addressed_count / merged_count` |
| `evidence` | JSON | Review Feedback or Pull Request Comment names behind a learned or suggested item. The form shows them as a table. |

### Knowledge Base Document

`repository`, `path` (`index.md` or `docs/<slug>.md`), `section` (index, docs, reverts), `title`, `content` (Markdown), `area`, `source_files` (JSON), `version_id`, `edited_by_human`, `generated_at`.

### Guard Integration

`integration_type` (Jira, Linear), `enabled`, `site_url`, `email`, `api_token` (Password), `linear_teams`, `instructions`.

## Conversations

### Chat Session

`title`, `user`, `kind` (Pull Request, Codebase, API), `pull_request`, `repositories` (JSON), `permission_mode` (Ask every time, Allow routine actions, Full access), `session_key`, `messages` (Table Chat Message: `role`, `content`, `context`, `sources`, `actions`).

### Chat Action

`chat_session`, `action_type`, `payload` (JSON), `status` (Proposed, Approved, Executed, Cancelled, Failed), `result`, `executed_by`.

## Operations

### Webhook Delivery

`code_provider`, `delivery_id` (unique), `event`, `action`, `repository_full_name`, `status` (Received, Processed, Ignored, Failed), `payload`, `error`, `attempt`. A daily job deletes rows older than 30 days. `attempt` counts how many times a lost job of the delivery was queued again ([02-architecture.md](02-architecture.md)).

## Indexes

Each index serves a known query. Rules:

- In a composite index, equality columns come first, then the range or sort column.
- A column that leads a composite index gets no single-column index. The composite index serves it.
- Do not wrap an indexed column in a function in `WHERE`. For example, use `col != ''`, not `IFNULL(col, '') != ''`. Both skip NULL.
- The graph tables take bulk writes, so add an index there only for a query on the review or index path.

`install.COMPOSITE_INDEXES` holds the composite indexes, and `install.UNIQUE_INDEXES` the indexes that also stop duplicate rows. `after_migrate` creates them, and replaces an index whose columns or uniqueness changed.

| Table | Composite index | Queries |
| - | - | - |
| Code Entity | `index, path(255)` | Outline of a file, deletes by path |
| Code Entity | `index, entity_name`; `index, qualified_name(255)` | Definitions, cross-repo `defines` and modules |
| Code Entity | `index, kind, path(255)` | Files, routes, routers, siblings in a folder |
| Code Edge | `index, source_entity, kind, line` | Callees in line order |
| Code Edge | `index, target_entity`; `index, target_name` | Callers, references, cross-repo symbol users |
| Code Edge | `index, kind, target_name` | Inherits edges for the symbol table, HTTP references |
| Code Edge, Code Chunk | `index, path(255)` | Deletes by path |
| Code Chunk | `index, entity` | Chunks of symbol hits |
| Code Chunk | FULLTEXT `content` | Lexical search |
| Code Review | `status, creation` | Queued reviews to resume, running slots, monthly credits, counts |
| Code Review | `repository, creation` | Reviews of a repository in a time window |
| Review Finding | `pull_request, status` | Open findings of a pull request |
| Review Finding | `repository, creation` | Noisy comment types |
| Review Finding | `status, posted` | Open findings number cards |
| Pull Request | UNIQUE `repository, number` | Lookup by provider number, one row for each pull request |
| Pull Request | `repository, state, merged_at`; `state, merged_at` | Polling of open pull requests, merged pull requests in a window |
| Pull Request Comment | `repository, author` | Feature tips |
| Knowledge Base Document | `repository, path` | Read and list documents |

Single-column indexes (`search_index`) cover the other lookups and the link fields that Connections tabs count.

## Roles

| Role | Access |
| - | - |
| Guard Admin | Everything. |
| Guard Member | Read all. Write custom context, chat sessions, own Guard User. Enable repositories of own teams. |
| Guard Viewer | Read analytics, repositories, pull requests, custom context. |
