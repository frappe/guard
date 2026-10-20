# Integrations

## Code providers

| Provider | Auth | Events |
| - | - | - |
| GitHub (github.com, Enterprise Server) | GitHub App (preferred) or a bot token | App webhooks, or a repository/organization webhook with a secret |
| GitLab (gitlab.com, self-managed) | Bot access token | Group or project webhook with a secret token |
| Gitea | Bot access token | Organization or repository webhook with a secret |
| Bitbucket Cloud | Workspace access token or app password | Workspace or repository webhook with a secret |

### Adapter interface

`guard.providers.base.ProviderAdapter`. One subclass for each provider. The review engine calls only this interface.

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
3. The webhook URL is `<site>/api/method/guard.webhooks.endpoint.github`.
4. The admin stores the app id, the slug, the private key and the webhook secret on the Code Provider.
5. Guard signs a JWT with the private key and gets one installation token for each team. It caches each token for 50 minutes.
6. An installation event creates the Guard Team and its repositories. New repositories get `reviews_enabled` from `auto_enable_new_repositories`.

### Webhook endpoints

| Provider | URL | Signature check |
| - | - | - |
| GitHub | `/api/method/guard.webhooks.endpoint.github` | `X-Hub-Signature-256` HMAC SHA-256 |
| GitLab | `/api/method/guard.webhooks.endpoint.gitlab` | `X-Gitlab-Token` equals the secret |
| Gitea | `/api/method/guard.webhooks.endpoint.gitea` | `X-Gitea-Signature` HMAC SHA-256 |
| Bitbucket | `/api/method/guard.webhooks.endpoint.bitbucket` | `X-Hub-Signature` HMAC SHA-256 |

Each endpoint finds the Code Provider from the `provider` query parameter, checks the signature, stores a Webhook Delivery (unique delivery id), queues the handler, and returns `200`. A repeated delivery id is ignored.

### Event mapping

| Guard event | GitHub | GitLab | Gitea | Bitbucket |
| - | - | - | - | - |
| open | `pull_request` opened, reopened, ready_for_review, labeled (trigger label) | MR open, reopen, draft to ready | `pull_request` opened, reopened | `pullrequest:created` |
| push or rebase | `pull_request` synchronize | MR update with `oldrev` | `pull_request` synchronized | `pullrequest:updated` with a new head |
| closed | `pull_request` closed | MR close, merge | `pull_request` closed | `pullrequest:fulfilled`, `pullrequest:rejected` |
| comment | `issue_comment`, `pull_request_review_comment` | Note on MR | `issue_comment`, `pull_request_comment` | `pullrequest:comment_created` |
| reaction | none (poll) | Emoji hook | none (poll) | none |
| default branch push | `push` | Push hook | `push` | `repo:push` |
| re-run | `check_run` rerequested | none | none | none |

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
