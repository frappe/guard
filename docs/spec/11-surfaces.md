# Surfaces

## Desk UI

Guard has no separate front end. One site serves one company, and every employee uses Frappe Desk.

### Guard workspace

- Shortcuts: Pull Requests, Code Reviews, Review Findings, Repositories, Custom Context, Learned Memories, Knowledge Base, Repository Clusters, Integrations, Guard Settings, Review Settings.
- Number cards: reviews this month, open findings, bugs caught this month, addressed rate, my open findings.
- Charts: reviews per day, bugs caught by severity, addressed rate per week.
- Links: the reports below, the `guard-pr` page and the `guard-chat` page.

### Forms

| DocType | Form additions |
| - | - |
| Pull Request | Overview HTML (summary, score, risk, findings, diagram). Buttons: Open review, Re-review, Fix all, Approve, Merge. |
| Code Review | Rendered summary, findings table, token and credit use. Buttons: Retry, Cancel. |
| Review Finding | Code excerpt, provider link, fix prompt with Copy, agent links. Buttons: Dismiss, Reopen. |
| Code Repository | Index status and counts. Buttons: Index now, Full rebuild, Build knowledge base, Explain config. |
| Custom Context | Usage, addressed rate, status reason and the evidence comments. Buttons: Improve with AI, Approve, Ignore, Retire, Restore. |
| Knowledge Base Document | Markdown editor. Rendered Mermaid. |
| Repository Cluster | Buttons: Use this, Discard (for suggestions). |
| Code Provider | Buttons: Test connection, Sync repositories, Show webhook URL. |
| Guard Settings | Button: Fetch models from the gateway. |

### Actions show at once

A button changes the record in its request, and the form shows the new state before the answer comes back. On an error, the form loads the saved record again. The provider follows when the request commits, and background jobs never undo the change ([05-review-engine.md](05-review-engine.md#status)).

| Action | At once |
| - | - |
| Cancel | Status Cancelled. The check ends, 👀 goes, and mentions get 😕. |
| Retry, Re-review | A new Queued review, a queued check on the head, and the older reviews cancelled. |
| Dismiss, Reopen | The finding status. The provider thread is resolved or reopened, and the check counts the open findings again. |
| Index now, Full rebuild, Build knowledge base | Index or knowledge base status Queued. |
| Use this, Discard, Approve, Ignore, Retire, Restore | The new status. |

Bulk Retry in the Code Review list changes the rows in its request, as above, but a `short` job makes the provider calls: up to 100 reviews make too many calls for one request.

Forms reload when the realtime events `guard_review_updated`, `guard_index_updated` and `guard_repository_updated` arrive.

### Connections

The Connections tab of a form lists the documents that link to it. The `links` in the DocType JSON define them.

| DocType | Connections |
| - | - |
| Code Provider | Guard Team, Code Repository, Webhook Delivery |
| Guard Team | Code Repository, Review Settings, Custom Context |
| Code Repository | Pull Request, Code Review, Review Finding, Pull Request Comment, Review Feedback, Repository Index, Knowledge Base Document, Repository Cluster |
| Repository Index | Code Repository (default index), Code Entity, Code Edge, Code Chunk |
| Pull Request | Code Review, Review Finding, Pull Request Comment, Review Feedback, Chat Session |
| Code Review | Review Finding, Sandbox Run |
| Review Finding | Pull Request Comment, Review Feedback, Sandbox Run |
| Custom Context | Review Finding |
| Chat Session | Chat Action |
| Guard User | Guard Team, Code Review, Chat Session, Chat Action |

Code Entity, Code Edge and Code Chunk connect only to Repository Index. Their tables have no index on `repository`, so a count from Code Repository would scan every row.

### Pages

| Page | Route | Purpose |
| - | - | - |
| `guard-pr` | `/desk/guard-pr/<pull request>` | Review page: list, overview, tour, full diff, chat ([07-conversations.md](07-conversations.md)) |
| `guard-chat` | `/desk/guard-chat` | Codebase chat |
| `guard-health` | `/desk/guard-health` | The state of Guard and how reviews ran, for Guard Admin and System Manager |

### Guard Health

The page shows the reviews that ended in the last 24 hours, 7 days or 30 days, and then the state now.

- Cards: throughput (completed reviews per hour), reviews in flight, review time and wait to start (median and 90th percentile), turns and rounds per review, turns per minute, the failed and retried share, tokens and the cached share, estimated cost, and findings per review.
- Charts, by hour for 24 hours and by day for longer: reviews that ended (completed, failed, cancelled), review time, wait to start, turns and rounds, turns per minute, tokens, and the mean seconds in each stage.
- A table for each tier, then the queues, the reviews created and why reviews failed in the period, the AI gateway and the indexes that need attention.

Time, turns and findings count the completed reviews. Tokens and cost count every review that ended, because failed and cancelled reviews spend them too. Turns per minute divides the turns by the time of the `review` stage. Reviews from before `stage_seconds` use their whole duration. Wait to start runs from the request to the start of the last attempt. Cost uses the prices in Guard Settings.

### Reports

| Report | Type | Content |
| - | - | - |
| Guard Analytics | Script | Summary cards, charts and leaderboards |
| Bugs Caught | Script | One row for each posted finding |
| My Guard | Script | The same metrics for the pull requests of the current user |
| Guard Usage | Script | Reviews, credits, input, cached and output tokens, cache share, agent turns and estimated cost, by day, tier or repository. Tokens count every attempt of every review. Credits count completed reviews, as the monthly cap does. The cost uses the prices in Guard Settings and is only an estimate: the gateway bills. Guard Admin and System Manager only, because it reads all teams. |

Frappe report export gives CSV and Excel.

## Analytics

### Filters

Team, repository, author, and period: this week, this month, this quarter, this year, all time, custom range.

### Cards

| Card | Definition |
| - | - |
| Total PRs | Reviewed pull requests merged in the period |
| Total Reviews | Completed Code Reviews in the period |
| Avg Merge Time | Mean of `merged_at - opened_at` for merged pull requests |
| Bugs Caught | Posted findings, and the number of repositories they are in |

### Charts and leaderboards

| Chart | Metrics | Leaderboard |
| - | - | - |
| PRs reviewed | PRs reviewed, total reviews, average reviews per PR, median and mean comments per PR, for each day | Top repositories by review count, or PRs with the most comments |
| Bugs caught | Posted findings for each day, stacked by severity, filter by severity | Repositories with the most bugs |
| Time to merge | Mean or median for each day | Top repositories by merge time |
| Top contributors | Merged PRs by author | Top contributors and their repositories |
| Addressed rate | Addressed findings divided by findings on closed or merged PRs, for each week, with the average | Top repositories by addressed rate |
| Comment ratings | Upvotes and downvotes for each week | Most upvoted comments |

A finding counts as addressed when a later commit fixed it or when it got positive feedback (Agree reply, or more upvotes than downvotes).

### Bugs Caught

Columns: finding, severity, security marker, repository, pull request, status (Open or Addressed), date. Filters: text, severity, security, status, repository, author.

## Query API

All methods need a logged-in session or a Frappe API token. Guard uses the stored provider credentials. Callers send no provider token.

A repository id is `<remote>:<branch>:<owner/repo>`, for example `github:main:acme/api`.

| Method | Path | Input | Output |
| - | - | - | - |
| Index | `POST /api/method/guard.api.v2.index_repository` | `remote`, `repository`, `branch`, `reload`, `notify` | `message`, `status_endpoint` |
| Status | `GET /api/method/guard.api.v2.get_repository` | `repository_id` | `repository`, `remote`, `branch`, `private`, `status`, `files_processed`, `num_files`, `sha` |
| Query | `POST /api/method/guard.api.v2.query` | `messages`, `repositories`, `session_id`, `stream`, `genius` | `message`, `sources` |
| Search | `POST /api/method/guard.api.v2.search` | `query`, `repositories`, `session_id`, `stream` | `sources` |

- `status` is one of `submitted`, `cloning`, `processing`, `completed`, `failed`.
- A source has `repository`, `remote`, `branch`, `filepath`, `linestart`, `lineend`, `summary`, `distance`.
- `stream: true` answers with server-sent events. Events: `source`, `delta`, `done`.
- `genius: true` uses the plus model and more turns.
- `session_id` keeps the conversation in a Chat Session of kind API.

## MCP server

- Endpoint: `POST /api/method/guard.mcp.server.handle`. JSON-RPC 2.0, MCP streamable HTTP in JSON response mode.
- Methods: `initialize`, `notifications/initialized`, `ping`, `tools/list`, `tools/call`.
- Auth: `Authorization: token <api key>:<api secret>`, or a Frappe OAuth bearer token.
- Every tool checks the permissions of the caller.

| Group | Tools |
| - | - |
| Account | `get_me`, `list_repositories` |
| Pull requests | `list_pull_requests`, `list_merge_requests`, `get_merge_request`, `list_merge_request_comments` |
| Reviews | `list_code_reviews`, `get_code_review`, `trigger_code_review` |
| Comments | `search_guard_comments` |
| Custom context | `list_custom_context`, `get_custom_context`, `search_custom_context`, `create_custom_context` |
| Knowledge base | `list_knowledge_bases`, `list_knowledge_base_documents`, `get_knowledge_base_document`, `search_knowledge_base` |
| Analytics | `list_analytics_filter_options`, `get_analytics_overview`, `list_analytics_findings` |
| Code | `search_code`, `get_callers`, `get_callees` |

- Repository identifiers: `name`, `remote`, `default_branch` together, or the namespace id (the Code Repository name).
- Knowledge base and comment text come back with `untrustedContent: true` and a notice.
- Errors use the MCP error format with a plain message.

## CLI

One Python file with no dependencies outside the standard library. Install:

```bash
curl -fsSL "<site>/api/method/guard.api.cli.download" -o ~/.local/bin/guard && chmod +x ~/.local/bin/guard
```

### Commands

| Command | Purpose |
| - | - |
| `guard login` | Browser sign-in through `/desk/guard-cli-login`. The CLI prints a one-time code, the page shows it, and the key goes only to `127.0.0.1` when the codes match. `--api-key` reads `key:secret` from stdin. |
| `guard logout`, `guard whoami` | |
| `guard review` | Review the current branch against its base. |
| `guard review show [ID]` | Show a review, or list recent reviews. |
| `guard review status [--commit SHA]` | Exit codes: 0 done, 1 none, 2 invalid, 3 running, 4 failed, 5 cancelled, 130 interrupted. |
| `guard config [PATH]` | Effective config with sources. `--json` |
| `guard settings [list\|get\|set\|unset\|path]` | Local settings: `color`, `site`, `review.output`, `review.layout`, `review.context`, `review.width` |
| `guard fix install\|status\|uninstall\|open URL` | Fix bridge |
| `guard update` | Download the newest CLI from the site |

### `guard review` options

`--branch`, `--resume`, `--include PATHS`, `--instructions TEXT`, `--plus`, `--apex`, `--json`, `--text`, `--agent`, `--layout comments|diff`, `--diff`, `--context N` (0 to 60, default 15), `--width N` (40 to 240), `--color`, `--no-color`.

- The CLI reviews committed changes that are not in the base branch. It ignores uncommitted changes.
- The CLI holds back files that look like secrets: names such as `.env`, `*.pem`, `*.key`, `id_rsa*`, `*credentials*`, and content with private key headers or known token shapes. `--include` sends them.
- CLI reviews run at base unless `--plus` or `--apex` is given.

## Fix with your agent

### In comments

Each inline comment links to the Review Finding in Desk. The summary footer links **Fix all** to the Pull Request.

### In Desk

The Review Finding form shows the fix prompt and these actions:

| Agent | Action |
| - | - |
| Claude Code | Open `guard-fix://` link (bridge runs `claude "<prompt>"` in the local repository) |
| Codex | Open `guard-fix://` link (bridge runs `codex "<prompt>"`) |
| Cursor | Open `cursor://anysphere.cursor-deeplink/prompt?text=<prompt>` |
| Conductor, Devin | Copy the prompt and open the app |
| Any | Copy the prompt |

The user picks agents in Guard User. Admins set default agents in Guard Settings.

### Bridge

1. `guard fix install` registers the `guard-fix://` URL handler. On Linux it writes a desktop entry and runs `xdg-mime`. On macOS it builds a small AppleScript app with `osacompile`.
2. A click on a `guard-fix://` link runs `guard fix open <url>`.
3. The CLI gets the prompt from the API.
4. The CLI finds the local folder for the repository in `~/.config/guard/repos.json`. If there is none, it asks once and saves the answer.
5. The CLI opens a terminal in that folder and starts the agent with the prompt.

The prompt has the file paths, line numbers, the finding text and the suggested code for every finding in the request.
