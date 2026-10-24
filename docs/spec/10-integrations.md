# Integrations

## GitHub

Guard supports one code provider: GitHub (github.com and GitHub Enterprise Server).

| Auth | Events |
| - | - |
| GitHub App (preferred) or a bot token | App webhooks, or a repository or organization webhook with a secret |

### Adapter interface

`guard.providers.base.ProviderAdapter` is the interface. `guard.providers.github.GitHubAdapter` is the only implementation. The review engine calls only the interface, and tests use a fake adapter.

| Group | Methods |
| - | - |
| Read | `get_pull_request`, `list_pull_request_commits`, `list_repositories`, `get_repository`, `list_comments`, `list_reactions`, `is_member` |
| Git | `clone_url`, `auth_header`, `pull_ref` |
| Write comments | `post_comment`, `update_comment`, `reply_to_comment`, `create_review`, `update_description` |
| Status and reactions | `set_status`, `add_reaction`, `remove_reaction` |
| Threads | `resolve_thread`, `unresolve_thread` |
| Pull request actions | `approve`, `dismiss_approval`, `request_changes`, `request_reviewers`, `merge`, `close`, `reopen`, `update_branch` |

An adapter raises `UnsupportedAction` for an action that the provider does not have. Callers handle it.

### GitHub App

1. The admin creates a GitHub App with these permissions: contents read, pull requests write, issues write, checks write, commit statuses write, members read, metadata read.
2. Events: pull request, pull request review, pull request review comment, issue comment, push, check run, installation, installation repositories.
3. The webhook URL is `<site>/api/method/guard.webhooks.endpoint.github_webhook?provider=<Code Provider>`.
4. The admin stores the app id, the slug, the private key and the webhook secret on the Code Provider.
5. Guard signs a JWT with the private key and gets one installation token for each team. It caches each token for 50 minutes. When GitHub answers 401 to a request with a cached token, Guard drops the token, gets a new one and sends the request once more.
6. An installation event creates the Guard Team and its repositories. New repositories get `reviews_enabled` from `auto_enable_new_repositories`.

### Webhook endpoints

| URL | Signature check |
| - | - |
| `/api/method/guard.webhooks.endpoint.github_webhook?provider=<Code Provider>` | `X-Hub-Signature-256` HMAC SHA-256 |

The endpoint finds the Code Provider from the `provider` query parameter, checks the signature, stores a Webhook Delivery (unique delivery id), queues the handler, and returns `200`. A repeated delivery id is ignored.

### Event mapping

| Guard event | GitHub event |
| - | - |
| open | `pull_request` opened, reopened, ready_for_review, labeled with a label from `labels` |
| push or rebase | `pull_request` synchronize |
| closed | `pull_request` closed (merged or not) |
| comment | `issue_comment` on a pull request, `pull_request_review_comment`, `pull_request_review` with a body |
| reaction | none: Guard polls reactions |
| default branch push | `push` |
| re-run | `check_run` rerequested |
| installation | `installation`, `installation_repositories` |

Guard tells push from rebase with `git merge-base --is-ancestor <before> <after>` after the fetch. If the old head is an ancestor of the new head, it is a push. Otherwise it is a rebase.

## Jira and Confluence

- Setup: Guard Integration of type Jira with the site URL, an account email and an API token. The same record reads Confluence on that site.
- Guard finds issue keys (`ABC-123`) and `/browse/` links in the title, the description and the branch name.
- Guard reads each issue: summary, description, status, type, acceptance criteria, and the last 10 comments.
- The agent can search Jira with JQL text search and Confluence with CQL text search through `lookup_ticket`.
- A finding that relies on a ticket or page ends with `Source used: <link>`. The summary lists every ticket and page under **Context used**.
- The connection is read-only.

## Linear

- Setup: Guard Integration of type Linear with an API key, optional team keys, and instructions.
- Guard finds identifiers (`ENG-123`) and `linear.app` links in the title, the description and the branch name.
- Guard reads each issue with GraphQL: title, description, state, comments, URL.
- If team keys are set, Guard reads only issues of those teams.
- The instructions go into the review prompt, for example "acceptance criteria are in the description".

When Jira and Linear are both on, Guard tries each one for a key and keeps the ones that resolve.

## Cross-repo context

### `context.repos`

A config can list related repositories. Each must be on the same provider and readable with the same credentials. Guard indexes them read-only (reviews stay off) and gives the agent the `repository` argument of `search_code`.

### Validation across repositories

Repositories are linked by code. No setup is needed. During a review, Guard checks the change against every other indexed repository of the same team, and also against `context.repos` and the members of active clusters.

The index records how repositories use each other:

- Python imports, with one edge for each imported name: `from a.b import c` is `a.b.c`.
- Frappe method paths in strings, and in URLs: `/api/method/a.b.c`.
- HTTP routes and their routers. A route keeps the name of its router. Guard resolves the full path when it reads the graph, because a router can be defined in another file.
- HTTP client calls: the path argument after an HTTP verb, also when the path is in a variable that is set a few lines before.

Paths are normalized: parameters become `*`. A client path matches a route when their ends agree, because clients and routers add different prefixes.

**Provider side.** When a pull request deletes or changes a function, method or route:

1. Guard finds the uses in the other repositories.
2. The uses appear in the blast radius ("2 uses in `org/pilot` may break") and in the Impact graph.
3. The reviewer gets each use with its line of code.
4. A use of a deleted entity, or a strong route match, surely breaks. Guard posts a P0 finding for it, and the verifier cannot lower a P0 at that entity.

**Consumer side.** When a pull request adds a call to another repository:

- A method path in the package of another repository must exist there. If not, Guard posts a P1 finding.
- A route call that belongs to a route family of another repository must match one of its routes. If not, Guard posts a P1 finding with the close routes.

### Repository clusters

- A Repository Cluster has 2 or more members.
- During a review of one member, every other member is cross-repo context.
- Repositories in `context.repos` come before cluster members when the context budget is tight.
- A cluster holds at most 20 GB of repositories.

### Suggested clusters

A weekly job reads commit authors of the last 90 days from each mirror. For each pair of repositories it computes the Jaccard index of the author sets. Pairs with an index of 0.3 or more and 2 or more shared authors join into suggested clusters. The confidence is the mean index. Admins click **Use this** (status Active) or **Discard**.

## Project rule files

When `include_rule_files` is on, Guard reads these files from the head commit and adds them as context:

- `CLAUDE.md` and `AGENTS.md` in the root and in each directory on the path of a changed file.
- `.cursorrules` and `.cursor/rules/*.md` / `*.mdc`.
- `.github/copilot-instructions.md`.

Every list item that starts with a word is a rule, and so is a line that says always, must, never, do not, mandatory or required. A rule has the id `project:<path>:<line>`. `CLAUDE.md` and `AGENTS.md` rules cover their own directory. Other rule files cover the repository. The reviewer gets the rules that cover the changed files as a checklist and reports each break. A break is a P2: fix it before merging, so it caps the confidence score at 4. It is P1 or P0 only when it is also a bug. A P2 rule break is posted at strictness 2 whatever its confidence, and of any comment type; strictness 3 drops it. Each comment links to the rule. Guard measures three kinds of rule in code, without the model, when a rule asks for them: the cyclomatic complexity of the Python functions that the change touches, the length of the files that it changes, and the prefixes of new boolean-returning Python functions. It takes the limit or the prefixes from the rule, and reports only what the change made worse. When a rule asks to add, update or write tests, and the pull request changes code under it but adds or updates no test under it, Guard reports a P2 finding without the model.
