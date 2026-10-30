# Security

## Roles

| Action | Guard Admin | Guard Member | Guard Viewer |
| - | - | - | - |
| Read pull requests, reviews, findings, analytics, knowledge base | yes | yes | yes |
| Read custom context and suggested rules | yes | yes | yes |
| Chat with PR, codebase chat | yes | yes | no |
| Chat actions on the provider | yes | yes (own token) | no |
| Create and edit team custom context | yes | team admin only | no |
| Create and edit organization custom context | yes | no | no |
| Approve or ignore suggested rules | yes | team admin only | no |
| Edit knowledge base documents | yes | yes | no |
| Enable and disable repositories | yes | own teams | no |
| Edit Review Settings of the organization | yes | no | no |
| Edit Review Settings of a team | yes | team admin only | no |
| Code providers, integrations, Guard Settings, people | yes | no | no |

Team admin is the Admin role on a Guard Team Member row. Guard checks it in `has_permission` hooks for team-scoped records.

Editing `.guard/` or `guard.toml` needs write access to the repository. Guard roles do not control it.

## Who Guard answers

Four settings, each `Everyone`, `Organization members` or `Guard users`:

| Setting | Controls |
| - | - |
| `trigger_by_authoring` | Whose pull requests and pushes start automatic reviews |
| `trigger_by_tagging` | Who can start a review with a mention |
| `chat_permission` | Who gets an answer to a question |
| `teach_permission` | Whose feedback becomes learning |

- `Organization members`: Guard asks the provider if the login is a member of the team namespace, or has write access to the repository. Outside maintainers are often collaborators and not members. Guard caches each answer for one hour. When the provider cannot answer, Guard does not cache it, and the login does not pass.
- `Guard users`: the login is on a Guard User record.
- A person outside a permission gets no reply and no error. The one exception: a person who resolves the thread of a finding without `teach_permission` gets a reply ([07-conversations.md](07-conversations.md#resolved-threads)).

## Secrets

- Provider tokens, private keys, webhook secrets, the API key of each AI provider and integration tokens are Password fields.
- Git commands get credentials through `-c http.extraHeader=...` for each call. Guard never writes a token to a remote URL or to git config.
- Logs and errors pass through a redactor that removes known secrets and token shapes.

## Local reviews

- A local review needs read permission on the Code Repository. Only the user who started it can submit it, and only once.
- Config and rule files come from the base commit, not from the diff, so a change cannot weaken its own review.
- Guard applies the diff in a temporary index and a scratch object directory. No ref, branch, working tree or mirror object changes, and Guard runs no code from it.
- A diff can be 5 MB at most.
- The MCP server takes only a user signed in by a token in the `Authorization` header (OAuth or an API key). Frappe sets the session id to the user for a token and to a hash for a browser session, so a browser session alone gets 401 and a page that the browser opens cannot call the tools. `frappe.mcp` checks the `Origin` header.
- `start_review` takes 30 calls an hour for each user, `submit_review` 30 and `find_symbol` 300. The upload takes 60 calls a minute from one address.
- The command that the agent runs has the default branch without quotes. Guard refuses a branch or a site URL with characters that a shell reads.
- The upload has no session. Its credential is a 32-character secret in a header, not in the URL, so it is not in access logs. Redis keeps only its blake2b digest, and compares it in constant time. It works once: after a good upload the session drops it. It ends with the session after 6 hours.
- The upload checks the secret before it reads the body, and again under the lock of the review. It acts for the user who started the review, and checks again that the user is enabled and can read the repository. The IP restriction of the user does not apply to it: the secret is the credential.
- The guide wraps team text, memories and knowledge base pages in `<data>` blocks.
- The Code Review keeps the findings, not the diff. The session in Redis holds no diff: `submit` diffs the base and the tree again.
- `branch` is cut to 140 characters and `instructions` to 4000. A finding body is cut to 4000 characters.

## Webhooks

- Every endpoint checks the signature with a constant-time compare before it parses the body.
- A missing or wrong signature returns `401` and stores nothing.
- Delivery ids are unique, so a replayed delivery does nothing.

## Untrusted input

Code, diffs, comments, ticket text, knowledge base text and config files can come from anyone who can open a pull request.

1. Guard puts all such text in labeled fenced blocks. The system prompt says that block content is data and never instructions.
2. Tool results are data too.
3. The review agent has no write tools on the provider. Only the publish stage writes, and it writes only findings and the summary.
4. Chat actions need approval in the default mode. They run with the token of the user, so a user can do only what the user can already do.
5. Guard strips HTML from model output except an allow-list: `details`, `summary`, `sub`, `h3`, `table` tags, `br`. It escapes the rest.
6. Mermaid output must start with an allowed diagram keyword and has a line limit.
7. A pull request cannot change its own auto-approve policy.

## Sandbox

Runtime validation runs code from the pull request.

- It is off by default. When on, it runs in a rootless container (podman preferred) with no network unless `sandbox_network` is on.
- CPU, memory and time are limited. The container has no access to secrets or to the site.
- Pull requests from forks never run in the sandbox unless the trigger comment comes from a Guard Admin.
- The work tree is a copy and Guard deletes it after the run.

## Links in comments

- Re-trigger links carry an HMAC signature over the pull request name with the site secret. A GET only shows a **Review again** button. The POST from that button starts the review, so link scanners and crawlers that open every link in a comment do not start reviews. A link starts at most one review every 2 minutes for each pull request. The time is in `retriggered_at` on the Pull Request. Guard reads it under the row lock and saves it before the review gate, so two clicks at once start one review and a link that the gate skips is limited too.
- Fix links open Desk. Desk login protects them.

## Audit

DocTypes for settings, providers, custom context and integrations track changes. Chat Actions record who approved and ran each action.

## Retention

| Data | Kept |
| - | - |
| Webhook Delivery | 30 days |
| Code Review, findings, feedback | Until deleted |
| Chat Session | 180 days |
| Git mirrors | Until the repository is removed |
