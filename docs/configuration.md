# Configure Guard

You can configure Guard in two places:

- **In the repository**, with files that you commit. Use this for settings and rules that belong to the code.
- **In Desk**, with **Review Settings**, **Custom Context** and **Code Repository**. Use this for defaults for all repositories.

A repository file overrides Desk for the same setting. Rules, notes and instructions from all places add up.

For the full rules, refer to the [configuration specification](spec/06-configuration.md).

## Quick start

Add `.guard/config.toml` to the root of the repository:

```toml
strictness = 2
ignore_patterns = ["**/*.min.js", "public/dist/**"]
exclude_authors = ["dependabot[bot]", "mergify[bot]"]
disabled_labels = ["skip-review"]

instructions = """
This is a Frappe app. Prefer frappe.qb over raw SQL.
Whitelisted methods must check permissions.
"""

[[rules]]
id = "no-raw-sql"
rule = "Do not put user input into SQL strings. Use query parameters or frappe.qb."
severity = "high"
```

Commit it on the branch of the pull request. Guard reads the settings from the head commit of each pull request, so you can try a change in the same pull request.

## Files in the repository

| File | Use |
| - | - |
| `.guard/config.toml` | Settings, rules, files and notes. |
| `.guard/rules.md` | Rules as plain markdown. |
| `.guard/files.toml` | Files that Guard reads as context for each review. |
| `guard.toml` | The same keys as `.guard/config.toml`, in one file. |

All files are optional. If a directory has both `.guard/` and `guard.toml`, Guard reads `.guard/` and ignores `guard.toml`.

Guard also reads `CLAUDE.md` and `AGENTS.md` in any directory, and `.cursorrules` and `.github/copilot-instructions.md` at the root. To stop this, set `include_rule_files = false`. Lines that say always, must or never are mandatory: breaking one is at least P1, and a rule that asks for tests flags code changes that touch no test file.

### Monorepos

You can put a `.guard/` folder or a `guard.toml` file in any directory. Guard walks from the root to the directory of each changed file:

- A setting in a lower directory replaces the value from a higher one.
- Rules, files, notes, instructions and ignore patterns add up.
- `disabled_rules` turns off a rule from a higher level, by its `id`.

Paths and scopes in a file are relative to the directory of that file.

### `rules.md`

Write the rules as plain markdown. Guard gives the whole file to the reviewer for changes to files below its directory. A list with one rule for each item is easy to read.

```markdown
- Every new DocType field needs a description.
- Use `frappe.throw` with a translated message, not `raise Exception`.
```

### `files.toml`

```toml
[[files]]
path = "docs/architecture.md"
description = "How the services talk to each other"

[[files]]
path = "db/schema.sql"
description = "Database schema"
scope = ["db/**"]
```

A file with a `scope` is read only when the pull request changes a file in that scope.

## Settings

### Review

| Key | Default | Meaning |
| - | - | - |
| `strictness` | `2` | `1` posts all real issues, also small style points. `2` drops P2 findings that Guard is not sure of. `3` posts only P0 and P1. |
| `comment_types` | all | Any of `logic`, `syntax` and `style`. |
| `effort` | unset | `base`, `plus` or `apex`. A higher tier uses a stronger model and more turns. `auto` lets Guard select: the first review of a pull request is at least `plus`. |
| `auto_review` | `["open", "push"]` | When Guard reviews without a request: `open`, `push`, `rebase`. `[]` means only when someone asks. |
| `review_drafts` | `false` | Review draft pull requests. |

After the first review, Guard posts only new P0 and P1 findings. A push that fixes a finding resolves its thread.

### Which pull requests Guard reviews

These filters apply to automatic reviews. A review that someone asks for skips them, except `exclude_authors` and `exclude_branches`.

| Key | Meaning |
| - | - |
| `labels` | Review only pull requests with one of these labels. |
| `disabled_labels` | Skip pull requests with one of these labels, for example `skip-review`. |
| `include_authors`, `exclude_authors` | Authors to review or to skip. |
| `include_branches`, `exclude_branches` | Target branches to review or to skip. |
| `include_keywords`, `ignore_keywords` | Words in the title or the description. Case does not matter. |
| `ignore_patterns` | Files that Guard does not review, in `.gitignore` syntax. |

Names and branches take `*`, `**`, `?` and `{a,b}`. Case does not matter. `dependabot[bot]` matches the literal name.

The skip lists (`exclude_authors`, `exclude_branches`, `disabled_labels` and `ignore_keywords`) add up across all levels. A repository cannot remove a skip that Review Settings sets. The other lists replace the value from a higher level.

### Output

| Key | Default | Meaning |
| - | - | - |
| `status_check` | `true` | Set a check named Guard. It passes only at 5/5 with no open findings. |
| `status_comments` | `false` | Without a status check, post "N files reviewed, no comments" on a clean review. |
| `update_summary_only` | `false` | Post the summary only, with no inline comments. |
| `update_description` | `false` | Put the summary in the pull request description. |
| `comments_outside_diff` | `true` | List findings on lines outside the diff in the summary. |
| `fix_with_ai` | `true` | Add a prompt for coding agents to each inline comment. |
| `hide_footer` | `false` | Remove the footer of the summary. |

The summary has these sections: `summary`, `issues_table`, `confidence`, `impact` and `diagram`. All are collapsed by default. To change one:

```toml
[sections.impact]
included = true
collapsible = true
default_open = true
```

### Context

| Key | Meaning |
| - | - |
| `instructions` | Free text for the reviewer. Instructions from all levels add up. |
| `[[rules]]` | `rule` (required), `id`, `scope`, `severity` (`low`, `medium`, `high`), `enabled`. |
| `disabled_rules` | Rule ids to turn off at this level and below. |
| `[[files]]` | Like `files.toml`. |
| `[[notes]]` | `content` (required), `scope`. Short facts, for example "This module is legacy." |
| `context.repos` | Related repositories, as `owner/repo`. Guard can search their code during a review. |

Guard also checks each change against the other repositories of the same team, with no setup. If a pull request removes a function, a method or a route that another repository uses, the summary says so and Guard posts a finding. If a pull request calls a method or a route of another repository that does not exist there, Guard posts a finding too.

## Approval

By default, Guard approves a pull request when the review is 5/5 and no finding is open. A new push dismisses the approval and starts a new review.

```toml
[auto_approve]
enabled = true
risk_ceiling = "medium"

[auto_approve.filters]
exclude_paths = ["**/patches/**", "hooks.py"]
exclude_authors = ["dependabot[bot]"]
```

| Key | Default | Meaning |
| - | - | - |
| `enabled` | `true` | Turn approval on or off. |
| `risk_ceiling` | `critical` | The highest risk level that Guard approves: `low`, `medium`, `high` or `critical`. |
| `filters.*` | | `include_paths`, `exclude_paths`, `include_authors`, `exclude_authors`, `include_branches`, `exclude_branches`, `labels`, `disabled_labels`, `include_keywords`, `ignore_keywords`. |

Guard reads `[auto_approve]` from the base branch, not from the pull request. A pull request cannot make the policy weaker for itself. When there are many configs, the strictest one wins.

Labels `do-not-merge` and `manual-review` always stop approval.

## Talk to Guard

Mention Guard in a pull request comment. Guard answers to the handles in **Guard Settings**, `@guard` by default, and to the name of the GitHub App, for example `@frappe-guard`.

| Comment | Result |
| - | - |
| `@frappe-guard review` | A new review. |
| `@frappe-guard review at apex` | A new review with the `apex` tier. |
| `@frappe-guard review only the API changes` | A review with these instructions. |
| `@frappe-guard is this thread-safe?` | An answer in the thread. |
| A reply in a Guard thread | Guard answers in the same thread. |
| 👍 or 👎 on a Guard comment | Feedback. Guard learns which findings the team rejects. |

You can also click **Re-trigger** in the footer of the summary.

## Settings in Desk

### Review Settings

**Review Settings** holds the defaults for all repositories. There is one record for the organisation. A team can have its own record. Clear **Inherit from Organization** to use it.

It has the same settings as the repository files. The **Filters** section has one list for each filter, with one value on each line:

| Field | Config key | Example |
| - | - | - |
| Skip Authors | `exclude_authors` | `mergify[bot]` |
| Skip Labels | `disabled_labels` | `skip-review` |
| Skip Target Branches | `exclude_branches` | `version-14*` |
| Skip Keywords | `ignore_keywords` | `(backport #` |
| Only Authors | `include_authors` | |
| Only Labels | `labels` | |
| Only Target Branches | `include_branches` | |
| Ignore Patterns | `ignore_patterns` | `**/*.min.js` |

For example, to skip the backport pull requests that Mergify opens, add `mergify[bot]` to **Skip Authors**. To skip only backports, add `(backport #` to **Skip Keywords**.

Review Settings also has:

- **Risk definitions**: extra text that tells the reviewer what each risk level means for your team.
- **Advanced Filter Rules**: rules with conditions, which can also set a tier. Use them only when the lists above are not enough.

Each filter rule has a list of conditions in JSON:

```json
[
  {"field": "target_branch", "operator": "include", "values": ["develop", "version-*"]},
  {"field": "files_changed", "operator": "less_than", "values": ["100"]}
]
```

- Fields: `author`, `target_branch`, `source_branch`, `label`, `repository`, `title`, `keyword`, `file_path`, `files_changed`.
- Operators: `include` and `exclude`. For `files_changed`: `more_than` and `less_than`.
- A rule without a tier is a gate. When gates exist, Guard reviews a pull request only if it matches one of them.
- A rule with a tier does not gate. When it matches, the review uses that tier or a higher one.

### Custom Context

A **Custom Context** record adds a rule, a file or a note from Desk, without a commit. Use **Repository Scope** (for example `frappe/*`) and **Path Scope** to limit it.

Guard also suggests rules from the review comments of your team. Suggested rules appear as Custom Context records with the status Suggested. Set the status to Active to use one.

Guard learns memories from replies, reactions and review comments. Open **Learned Memories** in the Guard workspace to read them and the comments behind each one. Click **Retire** to stop using a memory and **Restore** to use it again. Guard also retires a learned memory when the team keeps rejecting the findings that cite it.

### Code Repository

On a **Code Repository**, you can:

- turn reviews on or off with **Reviews Enabled**;
- set **Review Tier** and **Strictness** for that repository only.

## Check the configuration

- On a **Code Repository**, click **Explain config**. Enter a path. Guard shows the settings for that path and where each value comes from.
- With the `guard` command-line tool, run `guard config <path>`.
- If a config file has an error, Guard ignores the file and lists the error under **Config problems** in the summary comment.
