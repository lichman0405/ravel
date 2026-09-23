# 08 — Research Gateway & TUI

## 1. Product interaction

No normal Web Dashboard.

Users interact remotely with cloud RAVEL through a local TUI.

```text
Local Textual TUI
  -> HTTPS/WebSocket/Artifact API
Research Gateway
  -> RAVEL Runtime
  -> DSH
```

DSH is never internet-facing.

## 2. TUI responsibility

TUI is display/input/control only.

It does not:
- run scientific logic
- call DSH directly
- mutate DAG directly
- hold authoritative state
- decide review
- store research truth

## 3. Role views

### Project Owner / Research User
- create/select project
- natural-language conversation with Master
- current Master focus
- current phase/status
- read-only DAG
- decisions
- reviews
- evidence summaries
- compute/experiment state
- approval requests
- pause/resume
- modify Authority Envelope through controlled command
- upload relevant user artifacts

### Lab User
- assigned experiment tasks
- approved procedure
- allowed ranges
- required deliverables
- status
- report problem/deviation
- upload artifact
- receive updated instruction

The lab screen is served by `/projects/{project_id}/lab/*`, and its shape is the
execution contract's rather than a table of its own: the task is the node, the
instruction is the frozen contract, and the allowed ranges and required
deliverables are read out of it rather than restated. An *updated instruction* is
therefore a new contract version, which is why every response carries the version
and the freeze time.

Once a run has been handed to a bench, the same task carries the **package** the
bench was given — the documents preparation wrote, their hashes, and the
materializer that produced them — read back from the prepared directory rather
than re-rendered from the contract, so the protocol a bench worked from does not
change under it when a materializer is improved. Each document is served by a
name the package's manifest listed, and any other name is refused with the list
of what the package does hold.

The two writes are the two things a person at a bench can contribute that nobody
else can: **the file they were asked for** (`POST .../uploads?output=<name>`),
filed against a named output and refused if the handover owes no such name, and
**the observation that the plan and the bench disagreed**
(`POST .../deviations`). Both are recorded first and then delivered to the
waiting run, and a delivery that could not be made is reported in the response
rather than raised — everything is already on the record by then, and telling a
lab user their upload failed because a scheduler was down would be false.

### Admin
- DSH health
- Temporal health
- Runtime status
- backend status
- project runtime diagnostics
- session state
- recovery tools/log refs

Admin does not make scientific decisions.

## 4. TUI visual design

Technology:
- Python
- Textual
- small RAVEL design system

Style:
- professional dense console
- GitHub/Linear/VS Code-problems/htop spirit
- no cyberpunk
- no decorative hacker-green
- use semantic color only
- few borders
- whitespace + hierarchy + status symbols

Semantic tokens:
- background
- surface
- border
- text
- muted
- accent
- success
- running
- waiting
- blocked
- failed
- review

## 5. Primary Owner screen

Priority:
1. Master current focus
2. attention required
3. execution state
4. DAG
5. decisions/reviews/evidence

Master must feel present.

## 6. Transport

- REST/HTTPS: commands/query/login
- WebSocket: event stream / Master streaming response / status
- Artifact API: upload/download, preferably streaming or presigned flow through Gateway-controlled authorization

## 7. Reconnect

TUI stores only:
- gateway endpoint
- auth refresh token/secure credential as appropriate
- last event sequence
- local preferences

On reconnect:
- authenticate
- fetch current projection
- replay events after sequence
