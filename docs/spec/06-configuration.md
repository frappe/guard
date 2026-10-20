# Configuration

## Sources

From lowest to highest priority:

1. Organization Review Settings.
2. Team Review Settings, when the team does not inherit.
3. Repository overrides on Code Repository: `review_tier`, `strictness`.
4. Root config file: `.guard/` or `guard.json` at the repository root.
5. Nested config files, from the root down to the directory of the file.

Settings: the closest value wins. Rules, file references and instructions: all levels add up. A config can turn off a dashboard or parent rule by its id with `disabledRules`.

If `.guard/` and `guard.json` are in the same directory, Guard reads `.guard/` and ignores `guard.json`.

## Which branch Guard reads

- All settings come from the head commit of the pull request.
- `autoApprove` comes from the base branch, so a pull request cannot loosen its own policy.

## `.guard/` folder

```text
.guard/
├── config.json    settings and structured rules
├── rules.md       rules as plain markdown, scoped to this directory
└── files.json     files the reviewer reads for context
```

All three are optional.

### `config.json` fields

| Field | Type | Default | Meaning |
| - | - | - | - |
| `strictness` | 1, 2, 3 | 2 | 1 verbose, 3 critical only |
| `commentTypes` | string[] | all | `logic`, `syntax`, `style` |
| `effort` | string | unset | `base`, `plus`, `apex`, `auto` |
| `labels` | string[] | | Review only pull requests with one of these labels |
| `disabledLabels` | string[] | | Skip pull requests with one of these labels |
| `includeAuthors`, `excludeAuthors` | string[] | | Author filters |
| `includeBranches`, `excludeBranches` | string[] | | Target branch filters |
| `includeKeywords`, `ignoreKeywords` | string | | Newline-separated, matched against title and description |
| `ignorePatterns` | string | | Newline-separated, `.gitignore` syntax |
| `autoReview` | string[] | `["open"]` | `open`, `push`, `rebase`. Each implies the ones before it. `[]` is never. |
| `triggerOnUpdates` | boolean | false | Old form: `true` is `["open", "push", "rebase"]` |
| `triggerOnDrafts` | boolean | false | Review drafts |
| `skipReview` | `"AUTOMATIC"` | | Old form of `autoReview: []` |
| `statusCheck` | boolean | true | Post a status check |
| `statusCommentsEnabled` | boolean | | Post "N files reviewed, no comments" |
| `shouldUpdateDescription` | boolean | false | Put the summary in the description |
| `updateSummaryOnly` | boolean | false | No inline comments |
| `fixWithAI` | boolean | true | Prompt for AI agents block and fix link |
| `hideFooter` | boolean | false | No footer |
| `summarySection`, `issuesTableSection`, `confidenceScoreSection`, `diagramSection` | object | | `included`, `collapsible`, `defaultOpen` |
| `includeIssuesTable`, `includeConfidenceScore`, `includeDiagram` | boolean | | Short forms of `<section>.included` |
| `context.repos` | string[] | | Related repositories, `owner/repo` on the same provider |
| `instructions` | string | | Free-form instructions. Concatenated across levels. |
| `rules` | Rule[] | | Structured rules |
| `disabledRules` | string[] | | Rule ids to turn off at this level and below |
| `autoApprove` | object | | See below |

`sequenceDiagramSection` and `includeSequenceDiagram` are accepted as other names for the diagram fields.

### Rule

| Field | Required | Meaning |
| - | - | - |
| `rule` | yes | The instruction |
| `id` | no | Needed to turn the rule off in a child config |
| `scope` | no | Globs relative to the `.guard/` directory |
| `severity` | no | `low`, `medium`, `high` |
| `enabled` | no | Default true |

### `autoApprove`

| Field | Default | Meaning |
| - | - | - |
| `enabled` | false | |
| `riskCeiling` | `low` | `low`, `medium`, `high`, `critical` |
| `filters.includePaths`, `filters.excludePaths` | | Globs or plain directory paths. Both sides of a rename count. |
| `filters.includeAuthors`, `filters.excludeAuthors` | | |
| `filters.includeBranches`, `filters.excludeBranches` | | |
| `filters.labels`, `filters.disabledLabels` | | |
| `filters.includeKeywords`, `filters.ignoreKeywords` | | |
| `filters.includeRepositories`, `filters.excludeRepositories` | | |

`autoApprove.instructions` in a file is ignored. Risk definitions are a dashboard setting only.

### `rules.md`

Plain markdown. The whole file applies to files under the directory that holds the `.guard/` folder.

### `files.json`

```json
{
  "files": [
    { "path": "docs/architecture.md", "description": "System overview" },
    { "path": "schema.sql", "description": "Database schema", "scope": ["src/db/**"] }
  ]
}
```

Paths and scopes are relative to the directory that holds the `.guard/` folder. A file with a scope is read only when a reviewed file matches the scope.

## `guard.json`

`guard.json` takes the same fields as `config.json`. It can be in any directory and cascades the same way. It also takes:

| Field | Meaning |
| - | - |
| `customContext.rules` | `[{ "rule": "...", "scope": ["..."] }]` |
| `customContext.files` | `[{ "path": "...", "description": "...", "scope": ["..."] }]` |
| `customContext.other` | `[{ "content": "...", "scope": ["..."] }]` |
| `patternRepositories` | Other name for `context.repos` |

## Merge rules

### For one file

Walk from the root to the directory of the file. At each level:

- Scalar settings and arrays replace the parent value.
- Section objects merge field by field.
- `rules`, `files`, `customContext` items and `instructions` add to the parent list.
- `disabledRules` removes rules by id from the list so far.

### For the pull request

Each file uses its own resolved config for rules and context. The pull request uses one merged setting:

| Setting | Merge |
| - | - |
| `strictness` | Maximum |
| `effort` | Highest tier. `auto` counts only when no level sets a tier. |
| `commentTypes` | Union |
| `autoReview` | Union |
| Sections | Shown if any config shows them |
| Booleans | True if any config sets true |
| `skipReview` | Skip only if every touched config says `"AUTOMATIC"` |
| `autoApprove` | Strictest: `enabled` must be true everywhere, exclude lists union, include lists intersect, lowest `riskCeiling` |

## Glob matching

| Where | Syntax |
| - | - |
| Labels, authors, branches, repositories | `*` (one segment), `**` (any), `?`, `{a,b}`. Case-insensitive. `[`, `]` and a leading `!` are literal, so `dependabot[bot]` works. No negation. |
| Paths: `scope`, `ignorePatterns`, auto-approve paths, dashboard path scopes | `.gitignore` syntax |
| Repository scope of custom context | `owner/*`, `group/subgroup/*`, or an exact name |

## Review gate

Guard decides to review in this order. The first "no" stops it.

1. The repository has reviews enabled.
2. The author passes `trigger_by_authoring` (automatic events) or the commenter passes `trigger_by_tagging` (manual triggers).
3. The credit cap allows the tier.
4. For automatic events only:
   1. The pull request is not a draft, or drafts are on.
   2. The event is in the merged `autoReview`.
   3. `skipReview` does not apply.
   4. The config filters pass: labels, authors, branches, keywords.
   5. The dashboard filter rules pass (below).
5. For manual triggers: only `excludeBranches` and `excludeAuthors` apply.

### Dashboard filter rules

- A rule matches when all its conditions are true.
- Rules with no tier are **gate rules**. If any gate rule exists, the pull request must match at least one.
- Rules with a tier are **tier rules**. They do not gate. The highest matching tier counts in tier selection.

## Validation

Guard validates each config file against a JSON schema. A file with an error is ignored. The summary comment lists the error under "Config problems" so the author can fix it.

## Explain

`guard.config.resolver.explain(repository, ref, path)` returns the effective config for a path and the source of each value. The CLI `guard config <path>` and the API use it.
