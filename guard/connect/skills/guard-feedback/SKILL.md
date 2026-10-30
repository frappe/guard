---
name: guard-feedback
description: Tell Guard that a finding is wrong or intended, so it dismisses the finding and learns for later reviews. Use when the developer says "Guard is wrong", "false positive", "this is intended", "ignore this finding", "dismiss it", "teach Guard", or "Guard keeps flagging this". Needs the guard MCP server.
---

# Teach Guard with a verdict on a finding

Guard keeps the reason of the developer. It can make the reason a team memory, so the next reviews do not raise the same finding.

## Steps

1. Find the finding id. Call the Guard tool `pr_findings` for the branch (see the guard-fix skill), or take the id from a link to `/desk/review-finding/<id>`.
2. Show the developer the finding and the verdict that you will send. Wait for their yes.
3. Call `finding_feedback` with:
   - `verdict`: `wrong` when the finding is false, `intended` when it is true but on purpose, `reopen` to undo an earlier verdict.
   - `reason`: one or two sentences, in the words of the developer. Make it general enough to apply to later code. Good: "In app/api/**, user dicts are copies for one request, so changing them is safe." Bad: "Not a bug."
4. Tell the developer the answer of Guard.

## Rules

- Never send a verdict that the developer did not agree to.
- Do not use it for a finding that you fixed. Guard sees the fix on the next push.
