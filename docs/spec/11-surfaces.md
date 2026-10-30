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
| Guard Settings | Button: Fetch models, from the AI provider of each model field. The AI provider fields list only enabled AI providers. |
| AI Provider | Button: Fetch models. |

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

The page shows the reviews that ended in the last hour, 6 hours, 24 hours, 7 days or 30 days, and then the state now.

- Cards: throughput (completed reviews per hour), reviews in flight, review slots (running reviews against `max_concurrent_reviews`, the reviews that wait for a slot or for more pushes, and `reviews_per_worker`), review time and wait to start (median and 90th percentile), turns and rounds per review, turns per minute, the failed and retried share, tokens and the cached share, estimated cost, and findings per review.
- Charts, with a bar for each 5 minutes of an hour, 15 minutes of 6 hours, hour of 24 hours, or day of a longer period: reviews that ended (completed, failed, cancelled), review time, wait to start, turns and rounds, turns per minute, tokens, and the mean seconds in each stage.
- A banner when reviews wait for a free review slot.
- A table for each tier, then the queues (the dedicated review queues too, when the bench has them), the reviews created and why reviews failed in the period, the breaker of each AI provider, and the indexes that need attention. A banner names each AI provider that is down.

Time, turns and findings count the completed reviews. Tokens and cost count every review that ended, because failed and cancelled reviews spend them too. Turns per minute divides the turns by the time of the `review` stage. Reviews from before `stage_seconds` use their whole duration. Wait to start runs from the request to the start of the last attempt. Cost uses the prices of the tier of each review in Guard Settings.

### Reports

| Report | Type | Content |
| - | - | - |
| Guard Analytics | Script | Summary cards, charts and leaderboards |
| Bugs Caught | Script | One row for each posted finding |
| My Guard | Script | The same metrics for the pull requests of the current user |
| Guard Usage | Script | Reviews, credits, input, cached and output tokens, cache share, agent turns and estimated cost, by day, tier or repository. Tokens count every attempt of every review. Credits count completed reviews, as the monthly cap does. The cost of each review uses the prices of its tier in Guard Settings, so a row with several tiers adds the cost of each. It is only an estimate: the gateway bills. Guard Admin and System Manager only, because it reads all teams. |

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

## Terminal and coding agents

Guard has no CLI and no MCP server of its own. People and coding agents use [frappectl](https://github.com/frappe/frappectl), a REST API v2 client for any Frappe site. It signs in with OAuth or an API key and keeps the permissions of the user. Every DocType and whitelisted method is open to it, so Guard does not need a second API.

```bash
uv tool install frappectl
frappectl auth login https://<site> --oauth --name guard
frappectl -s guard doc list "Code Review" -f pull_request=<name> --limit 5
frappectl -s guard doc list "Review Finding" -f code_review=<id> -f status=Open --fields "path,start_line,severity,title,body"
frappectl -s guard api method/guard.api.ui.fix_prompt -f target=<pull request or finding>
frappectl -s guard api method/guard.api.ui.rereview -f pull_request=<name>
frappectl -s guard api method/guard.api.v2.search -F query="where do we retry webhooks" -F 'repositories:=["github:main:acme/api"]'
```

- `frappectl guide` explains the commands to an agent.
- MCP clients use the MCP server of Frappe at `<site>/api/v2/mcp`, with OAuth or an API key. Its tool `call_method` calls the same methods. For example: `claude mcp add --transport http guard https://<site>/api/v2/mcp`, then `claude mcp login guard`.
- On install, Guard sets **System Settings › Agent Instructions** when it is empty. It tells MCP clients to call `guard.api.local.guide` for a review before a push.
- Write methods (`rereview`, `set_finding_status`, ...) check the same permissions as in Desk.

## Local reviews

A coding agent reviews a change before it is pushed, with its own model. Guard gives it the rules, the graph and its check findings, and keeps the result ([05-review-engine.md](05-review-engine.md#local-reviews)).

| Method | HTTP | Arguments | Answer |
| - | - | - | - |
| `guard.api.local.guide` | GET | | The workflow, for the agent |
| `guard.api.local.start` | POST | `full_name`, `base`, `diff`, `branch`, `head`, `instructions` | `review_id`, `tree` (the sha of the reviewed tree), `findings` of the checks, `guide` |
| `guard.api.local.symbol` | GET | `full_name`, `name` | Definitions with callers and callees |
| `guard.api.local.submit` | POST | `review_id`, `findings`, `summary`, `confidence_score`, `confidence_reason`, `risk_level`, `risk_reason` | `review`, `url`, `kept`, `dropped` |

- `full_name` is `owner/name`, or a remote URL such as `git@github.com:owner/name.git`.
- The agent sends the diff from the shell, so the diff costs no tokens:

```bash
base=$(git merge-base HEAD origin/develop)
git diff --binary "$base" | jq -Rs --arg base "$base" '{full_name: "acme/api", base: $base, diff: .}' \
  | frappectl -s guard api method/guard.api.local.start --input -
```

- Untracked files are not in `git diff`. The guide tells the agent to run `git add -N` on the new files of the change.
- Over MCP, the diff is a tool argument, and the model writes it out. The guide allows this for small diffs only.
- A finding has `path`, `start_line`, `end_line`, `severity`, `comment_type`, `title`, `body`, and can have `category`, `suggestion`, `rule_id` and `confidence`.
- A git `pre-push` hook can run the review headless, for example with `claude -p "Run a Guard local review"`.

## Fix with your agent

### In comments

Each inline comment links to the Review Finding in Desk. The summary footer links **Fix all** to the Pull Request.

### In Desk

The Review Finding form shows the fix prompt and these actions:

| Agent | Action |
| - | - |
| Cursor | Open `cursor://anysphere.cursor-deeplink/prompt?text=<prompt>` |
| Conductor, Devin | Copy the prompt and open the app |
| Any, for example Claude Code or Codex | Copy the prompt |

The user picks agents in Guard User. Admins set default agents in Guard Settings.

The prompt has the file paths, line numbers, the finding text and the suggested code for every finding in the request.
