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

Guard has no CLI of its own. People and scripts use [frappectl](https://github.com/frappe/frappectl), a REST API v2 client for any Frappe site. It signs in with OAuth or an API key and keeps the permissions of the user. Every DocType and whitelisted method is open to it, so Guard does not need a second API.

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
- Coding agents use the MCP server of Guard for local reviews ([MCP server](#mcp-server)).
- Write methods (`rereview`, `set_finding_status`, ...) check the same permissions as in Desk.

## MCP server

Coding agents (Claude Code, Codex, Cursor, VS Code, ...) connect to `https://<site>/mcp` for local reviews ([05-review-engine.md](05-review-engine.md#local-reviews)). The agent of the developer is the reviewer and pays for the model. Guard gives it the rules, the graph and its check findings, and keeps the result.

**Connect.** The Desk page **Connect Your Agent** (`guard-agent`) has:

- One setup prompt for the agent. The agent adds the server at user scope with the command or the config file of its own client, and tells the developer how to sign in. The prompt names the usual mistakes: an overwritten config file, the top-level key of each client, a header or token where OAuth is used.
- The commands for Claude Code (`claude mcp add --transport http --scope user guard <site>/mcp`, then `/mcp` › Authenticate) and Codex (`codex mcp add guard --url <site>/mcp`, then `codex mcp login guard`).
- A status line. It turns green when the agent of the user calls Guard, and names the client. The page asks again every 5 seconds until then.
- A warning when an OAuth setting that the sign-in needs is off.
- The last 5 local reviews of the user.

**Sign-in.** The agent signs in with the OAuth of the site. A 401 has the `WWW-Authenticate` header of Frappe, which points to `/.well-known/oauth-protected-resource`. The client registers itself (dynamic client registration) and opens the browser once. The protected resource of Frappe is the site URL, so the Public URL in Guard Settings, when set, must have the same host. These OAuth Settings must be on, and are on by default: Show Protected Resource Metadata, Show Auth Server Metadata, Enable Dynamic Client Registration. An API key in `Authorization: token <key>:<secret>` also works.

**Protocol.** `guard/mcp/http.py` is a page renderer, so the address is short. It is stateless: one JSON-RPC message for each POST, one JSON answer, no SSE (GET gets 405). It uses the checks and envelopes of `frappe.mcp` and answers the revisions 2025-03-26, 2025-06-18, 2025-11-25 (`initialize`) and 2026-07-28 (`server/discover`). The `instructions` give the workflow of a local review. A request needs a user signed in by a token (OAuth or an API key): a browser session alone gets 401. A user without a Guard role can connect, and each tool answers that they need the Guard Member role. A 403 would make some clients sign in again. JSON-RPC batches (allowed by 2025-03-26) are not supported. There is no CORS answer, so browser-based clients such as the MCP Inspector need `allow_cors`.

| Tool | Arguments | Answer |
| - | - | - |
| `list_repositories` | | The site, the user and the repositories that they can review. The setup prompt calls it to check the connection. |
| `start_review` | `repository` (the `origin` URL or `owner/name`), `instructions`; without a shell also `diff` and `base` | `review_id` and `command`, the shell command that sends the diff. With `diff`: the guide at once. |
| `review_context` | `review_id` | The guide: rubric, rules, graph facts, check findings and how to submit |
| `find_symbol` | `repository`, `name` | Definitions on the default branch, with callers and callees |
| `submit_review` | `review_id`, `findings`, `summary`, `confidence_score`, `confidence_reason`, `risk_level`, `risk_reason` | `review`, `url`, `kept`, `dropped` |

- A failed tool is a tool result with `isError` and a message that says what to do next.
- A finding has `path`, `start_line`, `end_line`, `severity`, `comment_type`, `title`, `body`, and can have `category`, `suggestion`, `rule_id` and `confidence`.

**The diff.** The model does not write out the diff. `command` sends it from the shell:

```bash
cd "$(git rev-parse --show-toplevel)" && base=$(git merge-base HEAD origin/develop) && \
git diff --binary "$base" | curl -sS --fail-with-body -X POST \
  -H 'Content-Type: application/octet-stream' -H 'X-Guard-Upload: <secret>' \
  -H "X-Guard-Base: $base" -H "X-Guard-Head: $(git rev-parse HEAD)" \
  -H "X-Guard-Branch: $(git branch --show-current)" --data-binary @- '<site>/mcp/upload/<review_id>'
```

- The upload has no session. The one-time secret of the review is the credential, and the upload acts for the user who started the review. The answer is plain text for the shell, and names the next tool call. An error is a 4xx with `error: <what to do>`.
- Guard sends the bytes of the diff to `git apply` as they are, so a text file in another encoding than UTF-8 applies.
- The default branch goes into the command without quotes, so `start_review` refuses a branch with other characters than letters, digits and `._/-`.
- Untracked files are not in `git diff`. The instructions tell the agent to run `git add -N` on the new files of the change.
- Without a shell (Claude Desktop), the agent passes `diff` and `base` to `start_review`. The instructions allow this for small diffs only.
- A git `pre-push` hook can run the review headless, for example with `claude -p "Review my change with Guard"`.

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
