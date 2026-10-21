# Conversations

## Mentions

A comment that contains a trigger handle is a mention. The handles are the Trigger Handles in Guard Settings and `@<slug>` of each GitHub App. Guard ignores handles inside code blocks and quotes. Guard ignores comments from its own bot logins.

### Intent

Guard removes the handle and reads the rest of the text.

1. Rules match first:
   - Empty text, `review`, `re-review`, `review this`, `review this draft` → **review**.
   - `at apex`, `apex review`, `effort: plus` and similar → **review** with a requested tier.
2. If no rule matches, the fast model classifies the text:

| Intent | Example | Action |
| - | - | - |
| review | "review only the API changes" | Start a review. The text becomes instructions. |
| question | "is this thread-safe?" | Answer in the thread. |
| diagram | "generate a class diagram" | Reply with a Mermaid diagram of the asked type. |
| feedback | "this is intentional because..." | Store feedback, learn, react 👍, reply only if a question is in it. |
| ignore | text addressed to a person | Do nothing. |

### Reactions on the mention

Guard reacts on the comment that mentions it, so the author knows where the command stands:

1. 👀 when Guard accepts the command: a review is queued, or Guard starts to answer.
2. 👍 when the command is done: the review is complete, or the reply is posted. 👀 is removed.
3. 😕 when the review fails, is skipped or is cancelled, or the reply cannot be posted. 👀 is removed.

If Guard does not accept a review command, for example because reviews are off for the repository, it removes 👀. A review runs later in a job, so the mention waits in the cache for 24 hours under the pull request and the person who asked.

### Answers

`guard.conversation.agent.ConversationAgent` answers questions. It has the read tools of the review agent ([05-review-engine.md](05-review-engine.md)) and the pull request diff. For an inline comment it also gets the file, the line, and the finding of the thread. It replies in the same thread. It can write tests, explain code and suggest code.

The reply ends with **Sources** when the answer used tickets or knowledge base pages.

## Thread replies

Guard reads every new comment in a thread that it started, even with no mention.

The fast model classifies the comment:

| Class | Action |
| - | - |
| question, pushback, request for a change | Answer in the thread. |
| acknowledgment ("thanks", "fixed", "ok") | React 👍. No reply. |
| human-to-human (mentions another person, or answers another person) | Do nothing. |

Each comment in a Guard thread is also Review Feedback of kind Reply ([08-learning.md](08-learning.md)).

### Clarification questions

A finding can carry a question. When the author answers, Guard checks the answer against the code:

- If the answer removes the concern, Guard replies with a short note and sets the finding to Dismissed with reason "clarified".
- If not, Guard replies with what is still open.

## Desk review page

Desk page `guard-pr` at `/desk/guard-pr/<pull request>`. The Pull Request form has an **Open review** button.

```text
┌──────────────┬──────────────────────────────────────┬──────────────┐
│ Open PRs     │ Overview | Code (Tour / Full diff)   │ Chat with PR │
│ (J / K)      │                                      │ (Shift+C)    │
│              │ file tree + diff, findings inline    │              │
└──────────────┴──────────────────────────────────────┴──────────────┘
```

### Overview tab

Summary, confidence score, risk level, findings with status, diagram (rendered Mermaid), review history.

### Code tab

- **Tour**: one step at a time. Each step shows a title, an explanation, and the hunks of the step. Left and Right move between steps.
- **Full diff**: a file tree and the diff of the selected file. Findings show under their lines.

### Chat with PR

1. The user opens chat with **Chat with PR** or Shift+C.
2. The user clicks **Select context** or presses Shift+S. Then the user drags across lines, or clicks a finding or a file header. Each selection becomes a chip in the composer.
3. The user sends the question. Sending ends select mode. Escape and **Done** also end it.

Chat history is a Chat Session of kind Pull Request.

### Chat actions

The chat agent can propose actions with the `propose_action` tool.

| Action | Routine |
| - | - |
| Post a comment, reply to a comment | yes |
| Resolve a thread, reopen a thread | yes |
| Request a review | yes |
| Approve, request changes | no |
| Update the description, update the branch | no |
| Merge, close, reopen | no |
| Create a rule from the answer | no |

A proposed action shows under the answer with **Approve** and **Cancel**. The user can edit the text before approval. For routine actions the menu also has **Allow for this chat**.

The shield chip sets the permission mode for the chat:

| Mode | Runs without asking |
| - | - |
| Ask every time | Nothing |
| Allow routine actions | Routine actions |
| Full access mode | Everything |

Buttons and keys on the review page (approve, merge, close, reopen, draft or ready, update branch, request changes, comment on a line) run the action at once: the click is the approval. They are recorded as Chat Actions.

A new chat starts in **Ask every time**. A request to draft or preview an action never publishes it. When the user raises the mode, waiting actions that the new mode allows run at once.

Actions run with the provider token of the user from Guard User. Without a token, the action fails with a message that tells the user to connect an account. **Create a rule** runs in Guard and needs no token.

### Keyboard shortcuts

| Key | Action |
| - | - |
| J / K | Next / previous pull request |
| H / L | Previous / next tab |
| Left / Right | Previous / next tour step |
| / | Search |
| Shift+O | Switch open and closed list |
| Ctrl+B | Toggle sidebar |
| Shift+C | Open chat |
| Shift+S | Select chat context |
| Space | Select the focused pull request |
| Ctrl+A | Select all visible pull requests |
| G | Re-review |
| A | Approve |
| M | Merge |
| C | Close |
| D | Draft or ready |
| O | Reopen |
| X | Request changes |
| U | Update branch |
| F | Fix (copy the fix-all prompt) |
| R | Toggle reviewed on the hovered file |
| T | Comment on the hovered line |
| ? | Show shortcuts |

When pull requests are selected in the list, G, A, M, C, O, X, U and F act on the selection. D does not act on a selection. Chat shortcuts do not run while the chat input has focus.

## Tour generation

After a review, the fast model gets the list of hunks, each with an id, and the changed entities. It returns steps:

```json
[{ "title": "Add the token model", "explanation": "...", "hunks": ["h3", "h4"] }]
```

Every hunk must be in exactly one step. Guard adds missing hunks to a last step called "Other changes". Steps go in reading order: data model, core logic, interfaces, callers, tests. Guard stores the tour on the Pull Request with the head sha.

## Codebase chat

Desk page `guard-chat`. The user picks one or more indexed repositories and asks questions. The answer cites files and lines with links to the provider. The engine is the same as the query API ([11-surfaces.md](11-surfaces.md)). Each conversation is a Chat Session of kind Codebase.
