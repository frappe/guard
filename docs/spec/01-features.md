# Features

Each feature has an ID. Code, tests and commits refer to these IDs. A feature is done when every acceptance criterion passes.

## R: Reviews

### R1. Automatic pull request review

Guard reviews a pull request when a configured event happens.

- Events: `open` (opened, reopened, marked ready, trigger label added), `push` (a push that adds commits), `rebase` (a push that rewrites history).
- Each event implies the events before it. `["rebase"]` means all three. `[]` means never.
- Guard skips draft pull requests unless `review_drafts` is true or the trigger comment asks for a draft review.
- Guard skips a pull request that has merge conflicts with its target branch. A push that fixes the conflicts starts the review. A trigger comment reviews it anyway.
- Guard adds the 👀 reaction when it starts and replaces it with 👍 when it is done. On failure it adds 😕.
- A review on a medium pull request finishes in less than 5 minutes with the base tier.

### R2. Manual trigger

- A comment that contains a trigger handle (`@guard` by default, more handles in settings) starts a review.
- `@guard review this draft` reviews a draft.
- `@guard review this at apex`, `@guard apex review` and `@guard effort: plus` raise the tier for that review only. A comment cannot lower the tier.
- Free text after the handle becomes extra instructions for that review, for example `@guard check for memory leaks`.
- The **Re-trigger** link in the summary footer opens a page with a **Review again** button. The button starts a new review without a comment.
- A "re-run" on the provider status check starts a new review.

### R3. Codebase context

- Every review queries the graph for each changed function, class and file: callers, callees, imports, sibling implementations and similar code.
- The review states the impact of a change on its callers when the change alters a contract (signature, return type, raised errors, side effects).
- The review compares new code with similar existing code and flags differences in validation, error handling and query style.
- The reviewer can read any file at the head or the base commit, search the code, and walk the graph with tools.

### R4. Summary comment

The summary comment has these sections. Each section can be turned on or off, made collapsible and set open or closed.

1. **Summary**: what the pull request does, who it affects, why. Major improvements and issues.
2. **Confidence score**: 0 to 5, with a short reason. Plus and apex reviews show the tier next to the score.
3. **Findings table**: each unresolved finding with severity, file, a one-line title and a link to the inline comment.
4. **Important files changed**: a table of the main files with a one-line overview each.
5. **Diagram**: a Mermaid diagram. The type is sequence, entity relation, class or flow. Guard picks the type from what changed. No diagram for trivial changes.
6. **Context used**: tickets, pages and cross-repo files that the review read.
7. **Footer**: review counter ("Reviews (2)"), last reviewed commit with its message, a **Re-trigger** link, a **Fix All** link.

On a new review Guard edits the same summary comment. It does not post a second one. On the first review Guard posts the summary comment before the inline comments, so that it shows above them on the pull request. If `update_description` is true, Guard writes the summary into the pull request description between markers instead.

### R5. Inline comments

- Each finding becomes an inline comment on the changed line or line range.
- Each comment has a severity badge (P0, P1, P2), a comment type and a short title.
- Most comments have a suggested fix. The fix uses a GitHub `suggestion` block that the author can apply in one click.
- If `fix_with_ai` is true, each comment has a collapsed "Prompt for AI agents" block and a **Fix with your agent** link.
- A finding outside the diff goes into the summary under "Comments outside the diff", unless the setting turns this off.
- If `update_summary_only` is true, Guard posts no inline comments.
- The reviewer can attach a clarification question when the intent of a change is not clear.

### R6. Severity, comment types and strictness

- Severity: P0 must fix (security, data loss, crash), P1 should fix (bug, wrong behavior, edge case), P2 consider (quality, maintainability).
- `comment_types` limits the posted types to a subset of `logic`, `syntax`, `style`.
- Strictness 1 posts all findings. Strictness 2 drops low-confidence P2 findings. Strictness 3 posts only P0 and P1.

### R7. Review tiers

| Tier | Credits | Behavior |
| - | - | - |
| base | 1 | One agent pass with full graph context. |
| plus | 3 | More agent turns, a stronger model, and a verification pass that removes false positives. |
| apex | 10 | Per-area agent passes, a cross-cutting pass, the strongest model, and a verification pass with the plus model. |
| auto | 1, 3 or 10 | Guard picks a tier from size, risk and complexity. |

- The tier that runs is the highest of: dashboard tier, repository override, directory configs that the pull request touches, a matching tier rule, and a request in the trigger comment.
- `auto` counts as no tier in that comparison, except a matching tier rule set to `auto`.

### R8. Status check

- Guard posts a pending status when it starts and a final status when it is done.
- The status is `success` only when the review scores 5/5 and no finding is open on the pull request. Otherwise it is `failure`. After a review, this matches the auto-approve rule.
- When a person closes or reopens a finding (in Desk, or on its thread), Guard sets the status of the head again, from the last completed review of the head. It does not when the last review of the head is queued, runs or failed. It does not when that review found the check turned off (`status_check = false`). A newer skipped or cancelled review does not stop it, and a token install gets a new commit status. The score is then the cap of the findings that are open now ([05-review-engine.md](05-review-engine.md)), not the score of the review. Closing every open finding gives 5/5 and `success`. The Code Review keeps its own score. One exception: when a re-review of the head stops, the check gets back the stored score of the completed review, not the cap. The next change to a finding counts the open findings again.
- When Guard skips a review (the gate, no reviewable files, or the monthly credit cap), the status is `skipped` and gives the reason, so nobody waits for a review. A commit status has no skipped state, so token installs get `success`. GitHub counts a skipped check as passing.
- A skip does not replace the status of a commit that a review completed, failed or runs on. It also leaves the status alone while a review is queued and not cancelled, because that review fetches the newest head when it starts.
- A repository with reviews off gets no status. `status_check = false` turns the status off.
- With the status off and no findings, Guard posts a short "N files reviewed, no comments" note if `status_comments` is true.

### R9. Re-reviews and deduplication

- A re-review does not repost a finding that is still open on the pull request.
- A re-review marks old findings as outdated when their lines are gone.
- The review counter in the footer goes up by one.

### R10. Local and branch reviews

- The CLI sends a branch diff to Guard and gets a review back without a pull request.
- The result has the same findings, severity and summary as a pull request review.

### R11. Auto-approve

- Guard approves a pull request only after a review with score 5/5, no findings, and a risk level at or below the risk ceiling.
- Filters exclude pull requests by path, author, branch, label, keyword and repository.
- Draft, closed and merged pull requests, new commits after the review started, and the labels `do-not-merge` and `manual-review` block approval.
- Guard reads the policy from the base branch, so a pull request cannot loosen its own policy.
- New commits dismiss an earlier approval.
- Admins write natural-language risk definitions in the dashboard. Config files cannot set them.

### R12. Runtime validation

- When enabled, the reviewer can run commands in a sandbox container at the head commit.
- It can write targeted tests, run them and attach logs to a finding.
- Settings: enable, when to run (always, on request, never), container image, setup command, time limit, network on or off.

## C: Configuration

### C1. Dashboard settings

Organization-level review settings that every team inherits. A team can override them and can sync back to the organization.

### C2. Repository config files

- `.guard/config.toml`, `.guard/rules.md` and `.guard/files.toml` in any directory. Config is TOML.
- `guard.toml` at any directory level, for teams that want a single file.
- If `.guard/` and `guard.toml` are in the same directory, `.guard/` wins.
- Settings cascade from the root to the file. The closest value wins. Rules, files and instructions add up.
- A child config can turn off an inherited rule with `disabled_rules`.
- Guard reads all config from the head of the base branch, not from the source branch. A config change applies after it is merged.

### C3. Filters

Label, author, branch, keyword and path filters, with globs. Matching is case-insensitive. See [06-configuration.md](06-configuration.md).

### C4. Ignore patterns

`ignore_patterns` uses `.gitignore` syntax. Guard does not review matching files. The index still includes them.

### C5. Custom context

- Rules with scope (repository wildcards, path globs), severity and status (active, inactive, suggested).
- File references to docs in the repository.
- Free-form instructions and other context.
- Each rule shows its usage count and addressed rate.
- An **Improve with AI** action rewrites a vague rule into a specific one.

### C6. Project rule files

Guard reads `CLAUDE.md`, `AGENTS.md`, `.cursorrules`, `.cursor/rules/*` and `.github/copilot-instructions.md` from the head of the base branch as review context when they exist. Every list item that starts with a word is a rule, and so is a line that says always, must, never, do not, mandatory or required. A rule has the id `project:<path>:<line>`. `CLAUDE.md` and `AGENTS.md` rules cover their own directory. Other rule files cover the repository. The reviewer gets the rules that cover the changed files as a checklist and reports each break. A break is a P2: fix it before merging, so it caps the confidence score at 4. It is P1 or P0 only when it is also a bug. A P2 rule break is posted at strictness 2 whatever its confidence, and of any comment type; strictness 3 drops it. Each comment links to the rule. Guard measures three kinds of rule in code, without the model, when a rule asks for them: the cyclomatic complexity of the Python functions that the change touches, the length of the files that it changes, and the prefixes of new boolean-returning Python functions. It takes the limit or the prefixes from the rule, and reports only what the change made worse. When a rule asks to add, update or write tests, and the pull request changes code under it but adds or updates no test under it, Guard reports a P2 finding without the model.

### C7. Model and turn settings

Admins pick the LLM provider, the model for each tier, a fast model for small tasks, the embedding model, and the maximum agent turns for each tier.

## L: Learning

### L1. Reactions

👍, ❤️, 🎉 and 🚀 on a Guard comment count as up. 👎 and 😕 count as down. Other emoji are neutral. Each person counts once.

### L2. Replies

A reply to a Guard comment is feedback. Guard reads it with the finding and the thread, classifies it as agree, disagree, context or question, and learns from it. A disagree or context reply counts as a downvote.

### L3. Addressed detection

- When a push changes the flagged lines, Guard checks if the change fixes the finding. If yes, it marks the finding addressed and resolves the thread.
- When a pull request closes, Guard compares the first and last commit and marks findings addressed or ignored.

### L4. Memories

Guard turns context replies, repeated feedback, and human review comments on problems it missed into memories. A memory is custom context with source "Learned". The reviewer reads memories on later reviews.

### L5. Suppression

- Guard does not post a finding that matches past findings the team down-voted or ignored three or more times.
- Guard never suppresses P0 findings or findings with a security category.
- A comment type that the team almost never addresses (less than 10 percent over 10 or more comments) drops to strictness 3 for that repository.

### L6. Suggested rules

After about 10 merged pull requests, Guard reads human review comments and proposes rules with status "Suggested". Admins approve, edit or ignore them.

## K: Knowledge and context

### K1. Knowledge base

Each repository gets an `index.md` (architecture overview, glossary, file map, system diagram), one document for each main area or flow, and a reverts document. Guard keeps it in sync on pushes to the default branch. People can read and edit it.

### K2. Cross-repo context

- `context.repos` in config lists related repositories.
- A repository cluster groups repositories. Each member reads the others during reviews.
- Guard suggests clusters from shared contributors over the last 90 days.

### K3. Issue tracker context

Jira (with Confluence) and Linear. Guard finds tickets from the title, the description and the branch name. It reviews the code against the ticket and cites what it read.

## I: Interaction

### I1. Questions in comments

`@guard <question>` gets an answer in the thread, with codebase context. Examples: explain this file, write a test, generate a class diagram.

### I2. Thread replies

Guard answers follow-up comments in its own review threads without a mention. A classifier skips thanks, approvals and talk between people.

### I3. Chat with PR

A chat panel on the Pull Request form in Desk. The user can select code or review text as context. Chat can propose actions: comment, reply, resolve or reopen a thread, request review, approve, request changes, update the description, update the branch, merge, close, reopen, create a rule. Permission modes: ask every time, allow routine actions, full access.

### I4. Tour

A guided walk through the diff. Each step groups related hunks and explains them.

### I5. Codebase chat

Ask questions about one or more indexed repositories. Answers cite files and lines.

## S: Surfaces

### S1. Desk UI

Guard has no separate front end. Every screen is Frappe Desk:

- A **Guard** workspace with shortcuts, number cards and charts.
- Pull Request form: overview (summary, score, findings), tour, full diff, Chat with PR, review actions, keyboard shortcuts.
- Lists and forms for repositories, reviews, findings, custom context, knowledge base, clusters, integrations and settings.
- Script reports for analytics and for the findings table.
- **My Guard** stats: each employee sees reviews, findings and addressed rate for their own pull requests.

### S2. Query API

Index a repository, get its status, ask a question, search code.

### S3. MCP server

Tools for account, repositories, pull requests, reviews, comment search, custom context, knowledge base and analytics.

### S4. CLI

`guard review`, `review show`, `review status`, `config [path]`, `login`, `logout`, `whoami`, `settings`, `fix`.

### S5. Fix with your agent

Each comment links to the finding in Desk. The form gives a prompt, a copy button, deep links, and a local bridge command. **Fix All** sends every open finding at once. Supported agents: Claude Code, Codex, Cursor, Conductor, Devin.

### S6. Analytics

Summary cards, time series, leaderboards, a findings table, filters, CSV export.

## A: Administration

### A1. Organization and teams

One site is one organization. Each connected GitHub organization or user is a team. Teams inherit settings.

### A2. Roles

Guard Admin, Guard Member, Guard Viewer. Team admin and team member inside a team.

### A3. Who Guard answers

Four permissions, each "everyone", "organization members" or "Guard users": trigger by authoring, trigger by tagging, chat, teach.

### A4. Repositories

Enable and disable repositories. Auto-enable new repositories. Index status for each repository.

### A5. Usage limits

Count credits per review. Stop new reviews when the monthly cap is reached.

### A6. Feature tips

When on, Guard adds a short tip to a comment the first time a feature can help.
