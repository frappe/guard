# Knowledge base

Each indexed repository gets a knowledge base: a set of markdown documents that explain how the code works. People read and edit it in Desk. The reviewer reads it on every review. MCP clients read it too.

## Documents

| Path | Section | Content |
| - | - | - |
| `index.md` | index | Architecture overview, glossary, file map, system diagram |
| `docs/<area>.md` | docs | One document for each main area |
| `docs/flows/<flow>.md` | docs | One document for each main end-to-end flow |
| `docs/reverts.md` | reverts | Past reverts, rollbacks and incident fixes, and what to watch |

Each area and flow document has these sections: Purpose, How it works, Connections, Risk areas, Key files.

## Areas

Guard splits the repository into areas from the graph:

1. Start with the top-level directories.
2. If one area holds more than 40 percent of all entities and has subdirectories, split it into its subdirectories. Repeat to depth 3.
3. Merge directories with fewer than 3 files into the nearest parent area, or into "Other".
4. Keep at most 25 areas, the largest first.

## Flows

1. Find entry points: entities with callees and no callers, entities named by string references (routes, hooks, jobs), and handlers in files that match `*route*`, `*api*`, `*handler*`, `*controller*`, `*view*`, `*webhook*`.
2. Follow calls from each entry point to depth 4.
3. Keep the 8 entry points with the largest call trees.

## Build

1. For each area, the base model gets: the file list, the entity outline, the bodies of the 5 entities with the most callers, and the edges to other areas. It writes the area document.
2. For each flow, the base model gets the call tree with signatures and the entry point body. It writes the flow document.
3. The base model writes the overview and the glossary from the area documents.
4. Guard builds the file map and the system diagram from the graph. They need no LLM. The diagram is a Mermaid flowchart of areas, with the call count on each arrow.
5. For reverts, Guard reads `git log` on the default branch for commits that match revert, rollback, hotfix or incident, for the last 2 years, at most 50. The fast model summarizes each one: what broke, why, and what to watch.

With no gateway, Guard writes documents from the outline and the graph only.

Each build has a `version_id`: `<date>-<epoch ms>-<short hash>`. All documents of one build share it.

## Sync

1. A push to the default branch updates the index.
2. Guard maps the changed paths to areas and flows and marks them stale.
3. A job every 6 hours rebuilds stale documents, at most 10 for each repository each run.
4. If the set of areas changes, Guard rebuilds `index.md`.

## Human edits

An edit in Desk sets `edited_by_human`. On a rebuild, the model gets the edited text and keeps human-written statements. It changes only the facts that the code no longer supports.

## Use in reviews

The context builder adds the documents whose `source_files` overlap the changed paths, plus `docs/reverts.md`, at priority 4. The agent can search and read any document with its tools.
