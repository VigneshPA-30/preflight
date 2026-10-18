# Memory: what was decided, by whom, and what happened

Every simulation is saved as a **decision** in a memory graph in FalkorDB. Any agent, in any session, can recall earlier decisions
about a function (or about functions near it), claim a decision so that no other agent takes the same job, and report the outcome.

One memory graph per workspace (`mem_<project>_<hash>`), kept **separate** from the code graph. The code graph is deleted and rebuilt
whenever a file changes; the memory must survive that (a test checks it).

## Graph model

```
(:Decision {id, ts, target, winner, why, status, owner})-[:ABOUT]->(:Func {qname})
(:Decision)-[:CONSIDERED {rank}]->(:Plan {name, verdict, score, broken, silent, lying_tests, files, summary})
(:Plan)-[:SILENT_BREAK {status, reason, file, line}]->(:Func)       a break no test would notice
(:Decision)-[:HAD_OUTCOME]->(:Outcome {status, notes, agent, ts})   what an agent reported afterwards
```

Status flow: `proposed` -> `claimed` -> `applied` or `abandoned`. `proposed` again means "handed back".

## Tools

| Tool | What it does |
|---|---|
| `simulate_refactor_decisions` | Saves the run as a decision, and returns the earlier decisions about the same function (`memory.earlier_decisions`). |
| `recall_decisions` | Earlier decisions about a function **and functions within two calls of it** (marked `nearby`), or the latest ones. `status="open"` lists unfinished work. |
| `update_decision` | `claimed` (refused if another agent holds it), `applied` / `abandoned` (with notes), `proposed` (hand back). |

Recall follows the code graph: a decision about `Context.__init__` is found when you ask about `invoke.run`, a caller of it.

## A hand-over between two agents

1. Agent A simulates a change. A decision is saved (`proposed`). A claims it and starts work.
2. Agent B, in another session, calls `recall_decisions(status="open")`. It sees the decision, the plans compared, the silent breaks, and that A owns it.
3. B tries to claim it and is refused ("already claimed by A"). B picks other work, or A hands it back with `proposed`.
4. A reports `applied` with notes. Any later agent sees the outcome when it asks about that function or a neighbour.

## What is tested

- Separate server processes (three agent sessions) on one database share the memory (`tests/test_memory.py`).
- Eight agents claiming at the same moment: exactly one wins. The claim is a single Cypher query, so it cannot be split.
- The memory survives a rebuild of the code graph; two workspaces have separate memory.
- Data survives a normal exit and a killed server process.

## How this maps to the track (honestly)

| Track 02 focus | Status |
|---|---|
| Episodic memory | Yes: decisions and their outcomes |
| Shared state and handoffs between agents | Yes: claim, hand back, outcome |
| Separate memory per user or tenant | Yes: one graph per workspace, in one FalkorDB instance |
| High-concurrency reads and writes | Partly: an atomic claim is tested with 8 threads; no load test |
| Semantic memory | Only the code graph itself |
| Procedural memory, conversation history | No |

## Limits

- It remembers **decisions made with Preflight**, not conversations. It is advisory: an agent has to call the tools.
- One machine, one embedded database. No login: the workspace is the tenant.
- A claim is not released if an agent disappears; another agent hands it back by setting `proposed`.
