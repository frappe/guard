# Data model

All DocTypes are in the module "Guard". Field lists show the main fields only. Standard fields (`name`, `owner`, `creation`, `modified`) are not listed.

## Settings

### Guard Settings (single)

| Field | Type | Notes |
| - | - | - |
| `gateway_url` | Data | AI gateway base URL, for example a Grove gateway. |
| `gateway_api_key` | Password | |
| `gateway_dialect` | Select | `openai`, `anthropic` |
| `base_model`, `plus_model`, `apex_model` | Data | Model id for each tier, `<provider>/<name>`. |
| `fast_model` | Data | Classifiers, titles, small summaries. |
| `embedding_model` | Data | Blank means hash embeddings. |
| `embedding_dimensions` | Int | Size of the vector column. Changing it rebuilds vectors. |
| `max_turns_base`, `max_turns_plus`, `max_turns_apex` | Int | Agent turn limits. Defaults 10, 16, 24. |
| `input_price`, `cached_input_price`, `output_price` | Float | USD for one million tokens. Guard Usage uses them to estimate cost. `input_price` is for prompt tokens that the cache did not cover. 0 leaves that part out. |
| `trigger_handles` | Small Text | One handle each line. Default `@guard`. |
| `bot_logins` | Small Text | Provider logins that are Guard itself. Guard ignores its own comments. |
| `public_url` | Data | Base URL for links in comments. Default: the site URL. |
| `trigger_by_authoring`, `trigger_by_tagging`, `chat_permission`, `teach_permission` | Select | `Everyone`, `Organization members`, `Guard users` |
| `monthly_credit_cap` | Int | 0 means no cap. |
| `auto_enable_new_repositories` | Check | |
| `feature_tips` | Check | |
| `learn_from_activity` | Check | Default on. |
| `default_coding_agents` | Small Text | |
| `sandbox_enabled`, `sandbox_when`, `sandbox_runtime`, `sandbox_image`, `sandbox_setup_command`, `sandbox_timeout`, `sandbox_network` | mixed | Runtime validation. |

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
| `kb_status` | Select | `None`, `Building`, `Ready`, `Failed` |

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

`repository`, `number`, `title`, `description`, `author`, `state` (open, closed, merged), `is_draft`, `base_branch`, `head_branch`, `base_sha`, `head_sha`, `first_commit_sha`, `head_clone_url`, `web_url`, `labels` (JSON), `opened_at`, `merged_at`, `closed_at`, `additions`, `deletions`, `changed_files`, `review_count`, `last_reviewed_sha`, `summary_comment_id`, `latest_review`, `confidence_score`, `risk_level`, `guard_approved`, `approval_id`, `tour` (JSON), `tour_sha`, `provider_node_id`.

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
| `cached_tokens` | Int | The part of `input_tokens` that the gateway read from its prompt cache |
| `agent_turns` | Int | Model turns of the review agents |
| `review_number` | Int | 1 for the first review of a pull request. |
| `started_at`, `completed_at`, `error`, `body` | mixed | `body` is the rendered summary comment. |
| `stage`, `cancel_requested`, `attempt`, `retry_at` | mixed | Progress, cancellation, and the retry time after a busy gateway. A job every minute queues reviews whose `retry_at` has passed. |

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

`pull_request`, `repository`, `provider_comment_id`, `kind` (issue, review, inline), `thread_id`, `in_reply_to`, `author`, `is_bot`, `is_guard`, `body`, `path`, `line`, `commented_at`, `finding`, `processed`.

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

`code_provider`, `delivery_id` (unique), `event`, `action`, `repository_full_name`, `status` (Received, Processed, Ignored, Failed), `payload`, `error`. A daily job deletes rows older than 30 days.

## Roles

| Role | Access |
| - | - |
| Guard Admin | Everything. |
| Guard Member | Read all. Write custom context, chat sessions, own Guard User. Enable repositories of own teams. |
| Guard Viewer | Read analytics, repositories, pull requests, custom context. |
