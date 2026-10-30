# Configuration

Repository config is TOML. Guard parses it with the standard library `tomllib`.

## Sources

From lowest to highest priority:

1. Organization Review Settings.
2. Team Review Settings, when the team does not inherit.
3. Dashboard custom context: rules, files, instructions and notes from Custom Context records that match the repository.
4. Repository overrides on Code Repository: `review_tier`, `strictness`.
5. Root config: `.guard/` or `guard.toml` at the repository root.
6. Nested config, from the root down to the directory of the file.

Settings: the closest value wins. The skip lists (`exclude_authors`, `exclude_branches`, `disabled_labels`, `ignore_keywords`) and the check lists (`enabled_checks`, `disabled_checks`) are the exception: they add up across levels. Rules, files, notes, instructions and ignore patterns: all levels add up. A config can turn off a dashboard or parent rule by its id with `disabled_rules`.

If `.guard/` and `guard.toml` are in the same directory, Guard reads `.guard/` and ignores `guard.toml`.

## Which branch Guard reads

- All config comes from the head of the base branch: settings, rules, files, notes, instructions, ignore patterns and `auto_approve`. A pull request cannot turn on reviews, lift a skip, add instructions or loosen approval for itself.
- A branch made before a config change gets the current config.
- A config change in a pull request applies after it is merged.
- A CLI review has no base branch to trust. It reads the config that the CLI sends from its head commit.

## `.guard/` folder

```text
.guard/
├── config.toml    settings, rules, files and notes
├── rules.md       rules as plain markdown, scoped to this directory
└── files.toml     more files the reviewer reads for context
```

All three are optional. `guard.toml` takes the same keys as `config.toml`.

## Example

```toml
strictness = 2
comment_types = ["logic", "syntax"]
effort = "auto"
auto_review = ["open", "push"]
review_drafts = false
ignore_patterns = ["**/*.generated.*", "dist/**"]
exclude_authors = ["dependabot[bot]", "*-bot"]
include_branches = ["main", "release/*"]
instructions = """
This is a Frappe app. Prefer frappe.qb over raw SQL.
"""
disabled_rules = ["no-print"]

[sections.summary]
included = true
collapsible = true
default_open = false

[context]
repos = ["acme/shared-lib"]

[[rules]]
id = "no-raw-sql"
rule = "Use parameterized queries. Never interpolate user input into SQL."
scope = ["db/**"]
severity = "high"

[[files]]
path = "docs/architecture.md"
description = "System overview"

[[notes]]
content = "Legacy module. Be careful with changes."
scope = ["legacy/**"]

[auto_approve]
enabled = true
risk_ceiling = "low"

[auto_approve.filters]
exclude_paths = ["auth/**", "migrations"]
exclude_authors = ["dependabot[bot]"]
```

## Keys

### Review

| Key | Type | Default | Meaning |
| - | - | - | - |
| `strictness` | 1, 2, 3 | 2 | 1 verbose, 3 critical only |
| `comment_types` | array | all | `logic`, `syntax`, `style` |
| `effort` | string | unset | `base`, `plus`, `apex`, `auto` |
| `auto_review` | array | `["open", "push"]` | `open`, `push`, `rebase`. Each implies the ones before it. `[]` is never. |
| `review_drafts` | boolean | false | Review draft pull requests |

### Filters

| Key | Type | Meaning |
| - | - | - |
| `labels`, `disabled_labels` | array | Review only with one of these labels / skip with one of these labels |
| `include_authors`, `exclude_authors` | array | Author filters |
| `include_branches`, `exclude_branches` | array | Target branch filters |
| `include_keywords`, `ignore_keywords` | array | Matched against title and description, case-insensitive |
| `ignore_patterns` | array | `.gitignore` syntax, relative to the config directory |

### Output

| Key | Type | Default | Meaning |
| - | - | - | - |
| `status_check` | boolean | true | Post a status check |
| `status_comments` | boolean | false | Post "N files reviewed, no comments" when there is no status check |
| `update_description` | boolean | false | Put the summary in the description |
| `update_summary_only` | boolean | false | No inline comments |
| `fix_with_ai` | boolean | true | "Prompt for AI agents" block and fix link |
| `hide_footer` | boolean | false | No footer |
| `comments_outside_diff` | boolean | true | List findings outside the diff in the summary |
| `include_rule_files` | boolean | true | Read `CLAUDE.md`, `AGENTS.md` and similar files |
| `reuse_backport_reviews` | boolean | true | On a backport, do not review again the files whose changes Guard reviewed in the original pull request ([05-review-engine.md](05-review-engine.md#backports)) |
| `sections.<name>` | table | Collapsed | `included`, `collapsible`, `default_open`. Names: `summary`, `issues_table`, `confidence`, `impact`, `diagram` |

### Context

| Key | Type | Meaning |
| - | - | - |
| `instructions` | string | Free-form instructions. Concatenated across levels. |
| `[[rules]]` | array of tables | `rule` (required), `id`, `scope`, `severity` (`low`, `medium`, `high`), `enabled` |
| `disabled_rules` | array | Rule ids to turn off at this level and below |
| `enabled_checks` | array | Ids of Frappe review pack rules that are off by default, to turn on ([05-review-engine.md](05-review-engine.md#frappe-review-pack)). An unknown id is an error. |
| `disabled_checks` | array | Ids of Frappe review pack rules to turn off. It wins over `enabled_checks`. |
| `[[files]]` | array of tables | `path` (required), `description`, `scope` |
| `[[notes]]` | array of tables | `content` (required), `scope` |
| `context.repos` | array | Related repositories, `owner/repo` on the same provider |

Paths and scopes in a config are relative to the directory that holds `.guard/` or `guard.toml`. A rule, file or note with a scope applies only to files that match the scope.

### `auto_approve`

| Key | Default | Meaning |
| - | - | - |
| `enabled` | true | Approve each clean 5/5 review. A new push dismisses the approval and starts a new review. |
| `risk_ceiling` | `critical` | `low`, `medium`, `high`, `critical` |
| `filters.include_paths`, `filters.exclude_paths` | | Globs or plain directory paths. Both sides of a rename count. |
| `filters.include_authors`, `filters.exclude_authors` | | |
| `filters.include_branches`, `filters.exclude_branches` | | |
| `filters.labels`, `filters.disabled_labels` | | |
| `filters.include_keywords`, `filters.ignore_keywords` | | |
| `filters.include_repositories`, `filters.exclude_repositories` | | |

Risk definitions are a dashboard setting only. Config files cannot set them.

### `rules.md`

Plain markdown. The whole file applies to files under the directory that holds the `.guard/` folder.

### `files.toml`

```toml
[[files]]
path = "schema.sql"
description = "Database schema"
scope = ["db/**"]
```

## Merge rules

### For one file

Walk from the root to the directory of the file. At each level:

- Scalar settings and arrays replace the parent value, except the skip lists, which add to it. Section tables merge key by key.
- `rules`, `files`, `notes`, `instructions` and `ignore_patterns` add to the list so far.
- `disabled_rules` removes rules by id from the list so far.
- `enabled_checks` and `disabled_checks` add to the lists so far. A lower level cannot turn on a check that a higher level turned off.

### For the pull request

Each file uses its own resolved config for rules and context. The pull request uses one merged setting:

| Setting | Merge |
| - | - |
| `strictness` | Maximum |
| `effort` | Highest tier. `auto` counts only when no level sets a tier. |
| `comment_types` | Union |
| `auto_review` | Union |
| Sections | Shown if any config shows them |
| Booleans | True if any config sets true |
| Filters | From the root config |
| `auto_approve` | Strictest: `enabled` must be true everywhere, exclude lists union, include lists intersect, lowest `risk_ceiling` |

## Glob matching

| Where | Syntax |
| - | - |
| Labels, authors, branches, repositories | `*` (one segment), `**` (any), `?`, `{a,b}`. Case-insensitive. `[`, `]` and a leading `!` are literal, so `dependabot[bot]` works. No negation. |
| Paths: `scope`, `ignore_patterns`, auto-approve paths, dashboard path scopes | `.gitignore` syntax. A plain directory path also covers everything under it. |
| Repository scope of custom context | `owner/*`, `group/subgroup/*`, or an exact name |

## Review gate

Guard decides to review in this order. The first "no" stops it.

1. The repository has reviews enabled.
2. The author passes `trigger_by_authoring` (automatic events) or the commenter passes `trigger_by_tagging` (manual triggers).
3. The credit cap allows the tier.
4. For automatic events only:
   1. The pull request is not a draft, or `review_drafts` is on.
   2. The pull request merges into the target branch without conflicts (`git merge-tree` on the mirror).
   3. The event is in the merged `auto_review`.
   4. The config filters pass: labels, authors, branches, keywords.
   5. The dashboard filter rules pass (below).
5. For manual triggers: only `exclude_branches` and `exclude_authors` apply.

### Dashboard filter rules

- A rule matches when all its conditions are true.
- Rules with no tier are **gate rules**. If any gate rule exists, the pull request must match at least one.
- Rules with a tier are **tier rules**. They do not gate. The highest matching tier counts in tier selection.

## Validation

Guard parses each file with `tomllib` and checks the keys and types. A file with an error is ignored. The summary comment lists the error under "Config problems" so the author can fix it.

## Explain

`guard.config.resolver.ConfigResolver.explain(path)` returns the effective config for a path and the source of each value. The CLI `guard config <path>` and the API use it.
