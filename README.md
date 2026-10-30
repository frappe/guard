<div align="center" markdown="1">

<img src="guard/public/guard.svg" alt="Guard logo" width="80"/>
<h1>Guard</h1>

**AI Code Reviews That Read The Whole Codebase**

[![CI](https://github.com/frappe/guard/actions/workflows/ci.yml/badge.svg?branch=develop)](https://github.com/frappe/guard/actions/workflows/ci.yml)
[![Linters](https://github.com/frappe/guard/actions/workflows/linter.yml/badge.svg?branch=develop)](https://github.com/frappe/guard/actions/workflows/linter.yml)

</div>

<div align="center">
	<a href="docs/deployment.md">Deploy</a>
	-
	<a href="docs/configuration.md">Configuration</a>
	-
	<a href="docs/spec/00-overview.md">Specification</a>
</div>

## Guard

Guard reviews GitHub pull requests with an AI agent that reads the whole codebase, not only the diff. It builds a graph of every repository it reviews: definitions, calls, imports, inheritance, HTTP routes, and, for Frappe apps, doctypes, links, hooks and `frappe.call` strings. Each review uses the graph to find the callers that a change breaks, in the same repository and in the other repositories of the team, and the code that a change should match.

Guard is a Frappe app. One site serves one company. Everyone uses Frappe Desk.

### What it does

- Reviews pull requests on open, push or rebase, or when someone writes `@guard` in a comment.
- Posts a summary with a 0 to 5 confidence score, a risk level, a findings table, a diagram and a blast radius line, and inline comments with P0, P1 or P2 severity and suggested fixes.
- Reads team rules from `.guard/config.toml`, `.guard/rules.md`, `.guard/files.toml` and `guard.toml` in any directory, merged over dashboard settings.
- Checks each change against the project rules in `CLAUDE.md`, `AGENTS.md` and similar files, and reports each break as a P2 that links to the rule. It measures complexity, file-length and boolean-naming rules in code.
- Answers questions in pull request threads, and in Chat with PR and codebase chat in Desk.
- Learns from reactions, replies, fixes and human review comments. It suppresses findings that the team rejects, remembers problems that human reviewers caught and it missed, and suggests rules from human review comments.
- Builds a knowledge base for each repository and reads Jira, Confluence and Linear tickets.
- Resolves its own threads when the code fixes them, and, when a repository turns on auto-approve, approves a pull request after a clean 5/5 review.
- Runs targeted tests in a rootless container, when runtime validation is on.
- Serves a query API, and gives a fix prompt for each finding to paste into a coding agent.

## Install

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench --site <site> install-app guard
```

Guard needs MariaDB 11.7 or later for native vector search. Older databases work with a slower fallback. The `git` command must be on the path.

## Set up

For a production site with a GitHub App, follow [Deploy Guard](docs/deployment.md). The short version:

1. Add one or more **AI Provider** records, with their URL and key. Guard uses [Grove](https://github.com/frappe/grove), OpenRouter, Anthropic, OpenAI, or any other OpenAI-compatible or Anthropic-compatible endpoint. It can use several at the same time.
2. Open **Guard Settings** and set the **Default AI Provider**. A model can name another AI provider in the field next to it.
3. Click **Fetch models**, then set the model for each tier, the fast model and the embedding model.
4. Create a **Code Provider** of type GitHub. For a GitHub App, set the app id, the private key and the webhook secret. For a token, set the token and the webhook secret.
5. Copy the webhook URL from the Code Provider form into the GitHub App or the repository webhook. Select these events: pull requests, pull request reviews, review comments, issue comments, pushes, check runs and installations.
6. If GitHub cannot reach the site, turn on **Poll Providers** in Guard Settings instead.
7. Click **Sync repositories**, then enable reviews on each **Code Repository**. Guard indexes a repository when reviews are enabled.

Without an AI provider, Guard still indexes code, runs the graph checks, the secret scan and the Frappe checks, and posts a graph-only summary.

## Use

| Where | What |
| - | - |
| GitHub | `@guard`, `@guard review this at apex`, `@guard is this thread-safe?`, replies in Guard threads, 👍 and 👎 |
| Desk | The **Guard** workspace, **Pull Request Review** (`/desk/guard-pr`), **Codebase Chat**, **Guard Health** (queues, failures, the AI providers and the indexes), and the Guard Analytics, Bugs Caught and My Guard reports |
| Terminal and coding agents | [frappectl](https://github.com/frappe/frappectl): `frappectl auth login <site> --oauth`, then for example `frappectl doc list "Review Finding" -f status=Open` |

## Develop

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app guard
```

Tests run offline: a scripted model replaces the gateway, and temporary git repositories replace GitHub.

Evals replay recorded pull requests from `guard/evals/cases` through a full review, and need the AI gateway:

```bash
bench --site <site> execute guard.evals.run.run
```

This app uses `pre-commit` with ruff, prettier and eslint:

```bash
cd apps/guard
pre-commit install
```

## License

MIT
