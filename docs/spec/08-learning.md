# Learning

Learning makes reviews fit the team. It has five parts: feedback, addressed detection, memories, suppression and suggested rules. The setting `learn_from_activity` turns all five on or off. Only people who pass `teach_permission` can teach Guard.

## Feedback

| Source | How Guard gets it | Review Feedback kind |
| - | - | - |
| 👍 on a Guard comment | GitHub sends no reaction webhook. A job every 15 minutes reads reactions on findings of pull requests that changed in the last 14 days. | Upvote |
| 👎 on a Guard comment | Same | Downvote |
| A reply in a Guard thread | Webhook | Reply, with a sentiment |
| A push that fixes the finding | Addressed detection | Addressed |
| A merged or closed pull request with the finding still open | Close job | Ignored |

Guard keeps one Upvote or Downvote row for each person and finding. A removed reaction deletes the row.

The fast model gives each reply a sentiment: Agree, Disagree, Context, Question or Neutral. "Good catch, fixed" is Agree. "We do this on purpose because the webhook needs a quick answer" is Context.

## Addressed detection

### On each push

1. For each open finding on the pull request, check if the push changed lines in the finding range, with three lines around it.
2. Send each candidate to the fast model with the finding, the old code and the new code. The model answers `addressed` true or false.
3. If true: set the finding to Addressed, store the commit, resolve the thread on the provider, and add Review Feedback of kind Addressed.
4. A reply with sentiment Agree also marks the finding Addressed.

### On merge or close

1. Compare the first commit with the last head for each finding that is still open.
2. The fast model judges each one.
3. Addressed findings get Addressed. The rest get Ignored, and Guard adds Review Feedback of kind Ignored.

## Memories

A memory is Custom Context of type Pattern, source Learned, status Active.

1. Guard reads Review Feedback of kind Reply with sentiment Context or Disagree, and mentions with intent feedback.
2. The fast model writes one memory: the preference, why, and where it applies (paths and repository).
3. If an existing memory is close (cosine 0.9 or more), Guard adds the evidence to it. It does not create a new one.
4. Each memory links to its evidence comments.

Examples:

| Feedback | Memory |
| - | - |
| "Webhook handlers are sync on purpose. The provider needs an answer in 3 seconds." | In `webhooks/**`, synchronous handling is intended. Do not suggest async. |
| "@guard you missed a null check on line 45" | Check for null results from `get_user` before use. |

The reviewer reads memories that match the changed paths, at priority 3.

## Suppression

Before posting, Guard checks each finding against past findings in the same repository.

1. Skip this check for P0 findings and for the security category. Guard never suppresses those.
2. Find past findings with cosine 0.85 or more.
3. For these neighbors, compute: `score = upvotes + addressed - 2 × downvotes - ignored`.
4. Suppress the finding when there are 3 or more neighbors and `score < 0`.
5. Also suppress when one neighbor has cosine 0.93 or more, at least one downvote and no upvote.

The finding stays as a Review Finding with status Suppressed and the neighbor names in `status_reason`.

### Adaptive noise filter

For each repository and comment type, Guard counts the findings of the last 90 days. When the count is 10 or more and less than 10 percent were addressed, Guard drops P2 findings of that type. The Code Review records this in `config_snapshot`.

## Suggested rules

A weekly job runs for each repository with 10 or more merged pull requests since the last run.

1. Collect human review comments from merged pull requests of the last 60 days. Leave out bot comments and comments shorter than 20 characters.
2. The plus model groups the comments into themes. A theme needs 3 or more comments, on 2 or more pull requests.
3. Each theme becomes a rule with a scope and evidence.
4. Guard drops rules that are close to existing Custom Context (cosine 0.88 or more).
5. The rest become Custom Context with status Suggested and source Suggested.

Admins approve (status Active), edit, or ignore (status Ignored) each suggestion.

## Rule metrics

| Field | Meaning |
| - | - |
| `usage_count` | Posted findings that name this rule |
| `merged_count` | Those findings on merged pull requests |
| `addressed_count` | Those of `merged_count` that were addressed |
| `addressed_rate` | `addressed_count / merged_count`, blank while `merged_count` is 0 |

## Auto-approve

Guard approves a pull request when every condition is true:

1. The merged auto-approve policy is enabled. The policy merges dashboard settings with the base-branch config files, strictest value first.
2. The confidence section is on.
3. The review score is 5 and the review has no posted finding.
4. The risk level is at or below `risk_ceiling`. The agent sets the risk level with the risk definitions from the dashboard in its prompt.
5. The auto-approve filters pass, with both sides of each rename checked against path filters.
6. The regular review filters pass.
7. The pull request is open, not a draft, and its head is still the reviewed commit.
8. The pull request has no `do-not-merge` and no `manual-review` label.

If any condition fails, Guard does not approve and does not comment. The approval review has no body.

When new commits arrive, Guard dismisses its earlier approval. The next passing review approves again.

| Risk | Typical change |
| - | - |
| low | Docs, tests, styles, small local code changes |
| medium | Normal application logic |
| high | Dependencies, build or runtime config, shared core modules |
| critical | Auth, secrets, billing, migrations, infrastructure, CI, public APIs |
