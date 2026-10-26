# Learning

Learning makes reviews fit the team. It has six parts: feedback, addressed detection, missed issues, memories, suppression and suggested rules. The setting `learn_from_activity` turns all five on or off. Only people who pass `teach_permission` can teach Guard.

## Feedback

| Source | How Guard gets it | Review Feedback kind |
| - | - | - |
| 👍, ❤️, 🎉 or 🚀 on a Guard comment | GitHub sends no reaction webhook. A job every 15 minutes reads reactions on findings of pull requests that changed in the last 14 days. | Upvote |
| 👎 or 😕 on a Guard comment | Same | Downvote |
| A reply in a Guard thread | Webhook | Reply, with a sentiment |
| A push that fixes the finding | Addressed detection | Addressed |
| A merged or closed pull request with the finding still open | Close job | Ignored |
| A human comment on a problem that Guard missed | Webhook, see [Missed issues](#missed-issues) | Missed |

Guard keeps one Upvote or Downvote row for each person and finding. A removed reaction deletes the row. Guard skips reactions from its bot logins and from people who fail `teach_permission`.

The fast model reads each reply with the finding and the earlier replies of the thread. One call gives the class that decides how Guard answers ([07-conversations.md](07-conversations.md#thread-replies)) and a sentiment: Agree, Disagree, Context, Question or Neutral. "Good catch, fixed" is Agree. "Will do" is a promise, not a fix, so it is Neutral. "We do this on purpose because the webhook needs a quick answer" is Context.

The `upvotes` and `downvotes` of a finding count people. A person with a 👎 or 😕, or a reply with sentiment Disagree or Context, is one downvote. A rejecting reply does not close the finding, so the pull request author cannot clear a finding by replying. To close a finding that the team accepts without a fix, a person resolves its thread ([07-conversations.md](07-conversations.md#resolved-threads)).

## Addressed detection

### On each push

1. For each open finding on the pull request, check if the push changed lines in the finding range, with three lines around it.
2. Send each candidate to the fast model with the finding, the old code and the new code. The model answers `addressed` true or false.
3. If true: set the finding to Addressed, store the commit, resolve the thread on the provider, and add Review Feedback of kind Addressed.
4. A reply with sentiment Agree also marks the finding Addressed and resolves the thread.

### After each review

Guard checks each open finding of earlier reviews on the pull request.

1. If the new review reported the same issue again, the finding stays open.
2. If the file or the lines are gone at the new head, the finding becomes Outdated.
3. If the head did not change since the finding's review, the finding stays open. The model would read the same code.
4. Otherwise, the fast model reads the finding, the diff since the finding's review and the current code, and answers `applies`, `fixed` or `invalid`. Guard sends these checks at the same time, up to 4 at once. Their tokens count in the review.
   - `fixed`: the finding becomes Addressed.
   - `invalid`: the finding becomes Outdated.
5. Guard resolves the provider thread of each finding that it closes.

On GitHub, resolving a thread needs the App permission **Contents: Read & write**.

### On merge or close

1. Compare the first commit with the last head for each finding that is still open.
2. The fast model judges each one.
3. Addressed findings get Addressed. The rest get Ignored, and Guard adds Review Feedback of kind Ignored.

When the AI gateway is busy or down, the check runs again later ([02-architecture.md](02-architecture.md#processes-and-queues)). A push check runs again from the first push that was not checked up to the head. If the pull request closed in the meantime, the close check runs instead.

## Missed issues

A human review comment can raise a problem that Guard did not. Guard checks a comment when all of these are true:

1. Learning is on.
2. The comment is an inline comment that starts a thread, not a reply.
3. The author is not the pull request author and passes `teach_permission`.
4. The pull request has a completed Guard review.

The fast model reads the comment, the code around its line at the pull request head, and the posted Guard findings on the file. When the comment raises a bug, a risk or a required change that no finding covers, Guard adds Review Feedback of kind Missed. When the problem applies beyond this change, the same answer gives a memory, which Guard stores as below.

## Memories

A memory is Custom Context of type Pattern, source Learned, status Active.

1. Guard reads Review Feedback of kind Reply with sentiment Context or Disagree, mentions with intent feedback, and Missed feedback.
2. The fast model writes one memory: the preference, why, and where it applies (paths and repository).
3. If an existing memory of the same repository is close (cosine 0.9 or more), Guard adds the evidence to it. It does not create a new one. Rules never absorb a memory.
4. Each memory links to its evidence comments.

Examples:

| Feedback | Memory |
| - | - |
| "Webhook handlers are sync on purpose. The provider needs an answer in 3 seconds." | In `webhooks/**`, synchronous handling is intended. Do not suggest async. |
| "@guard you missed a null check on line 45" | Check for null results from `get_user` before use. |

The reviewer reads memories that match the changed paths, at priority 3. Each memory has the rule id `memory:<name>`. A finding that acts on a memory cites it, so the finding links to the memory in `custom_context` and the memory gets [rule metrics](#rule-metrics).

### Review and retire

The **Learned Memories** shortcut in the Guard workspace lists them. The form shows each evidence comment with its author, kind and pull request. **Retire** sets the status to Inactive and **Restore** sets it back to Active. The reviewer reads only Active memories. New feedback close to a retired memory adds evidence to it and leaves it retired.

A daily job retires a learned memory when, since `active_since`, the team rejected 3 or more of the findings that cite it and rejected more than it accepted. A finding is rejected when it has more downvotes than upvotes, or Guard marked it Ignored. It is accepted when it was addressed or has more upvotes than downvotes. A memory that only tells Guard what not to flag produces no findings, so only a person can retire it.

Every status change writes `status_reason`: who changed it, or the numbers behind an automatic retirement. Setting a memory Active again sets `active_since`, so the rejections from before do not count.

## Suppression

Before posting, Guard checks each finding against past findings in the same repository.

1. Skip this check for P0 findings and for the security category. Guard never suppresses those.
2. Find past findings with cosine 0.85 or more.
3. For these neighbors, compute: `score = upvotes + addressed - 2 × downvotes - ignored`. Rejecting replies count in `downvotes`.
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
| `addressed_rate` | `addressed_count / merged_count`, 0 while `merged_count` is 0 (a Percent column cannot be blank) |

## Auto-approve

Guard approves a pull request when every condition is true:

1. The merged auto-approve policy is enabled. The policy merges dashboard settings with the base-branch config files, strictest value first.
2. The confidence section is on.
3. The review score is 5, the review has no posted finding, and no finding of an earlier review on the pull request is still open, inline or only in the summary.
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
