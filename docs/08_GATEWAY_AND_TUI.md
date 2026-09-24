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

## 8. Language

Every screen the console draws is available in English and in Chinese, and the
reader switches between them with `F2` at any point, including on the sign-in
form. `RAVEL_TUI_LANGUAGE=en|zh` decides which one a deployment starts in;
anything else is refused at startup rather than silently fallen back from.

Translation covers what the console *says*: panel titles, column headers, tab
names, placeholders, empty-panel sentences, notice lines and the key hints along
the bottom. It does not cover what the rest of the system *calls* things —
node and project statuses, review outcomes, the five agent roles, backend and
service names, identifiers, timestamps, model and exception text. Those strings
are the same in the API, the database and the logs, and a console that renamed
them would be one whose reader cannot match what they see to what an operator
can be told over the phone.

So a Chinese screen reads `科学 DAG`, `需要你处理` and `暂停`, and still reads
`RUNNING`, `PROJECT_OWNER` and `WAITING_DECISION`.

Two consequences worth stating as requirements rather than as details:

- **A sentence has exactly one home.** All of them are in
  `src/ravel/tui/i18n.py`, as `key: (english, chinese)` pairs, and no other
  module under `ravel/tui` may contain a sentence — a unit test walks the
  syntax trees of the rest to prove it. A message cannot exist in one language
  and be missing from the other, because the shape of the data is the
  invariant.
- **The language is process state, not stored state.** It is read once at
  startup and lives for the session; nothing is written to disk, and there is
  no per-user preference file. A console that remembered a keystroke would be
  inventing a second kind of state for the TUI to hold, which §2 forbids it.
- **What the console says is translated; what it *sends* is not.** One rule and
  not two, and the difference is who the string is for. A deviation's kind is a
  term the record keeps (`parameter`), while the words beside it in the picker
  are a sentence a bench reads; a member's role is sent as `PROJECT_OWNER`,
  while the line above it says `项目负责人`. The case that looks like an
  exception is the same rule: the objective the console writes for a project
  opened from here — `Opened from the console; no objective has been stated
  yet.` — *is* stored text, and it is stored in the language of the person who
  opened it, because a person who had stated an objective would have stated it
  in their own language. This is that sentence with the objective left out, so
  it is written in the language of whoever the objective would have come from.
