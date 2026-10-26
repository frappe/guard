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

- `Organization members`: Guard asks the provider if the login is a member of the team namespace. It caches the answer for one hour.
- `Guard users`: the login is on a Guard User record.
- A person outside a permission gets no reply and no error.

## Secrets

- Provider tokens, private keys, webhook secrets, the gateway key and integration tokens are Password fields.
- Git commands get credentials through `-c http.extraHeader=...` for each call. Guard never writes a token to a remote URL or to git config.
- Logs and errors pass through a redactor that removes known secrets and token shapes.
- The CLI stores its API key in `~/.config/guard/credentials` with mode 0600.

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
