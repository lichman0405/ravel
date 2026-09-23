# 05 — Real Research & Evidence

## 1. Absolute rule

**Research evidence must be real.**

V0 compute/lab can be mocked. Web/academic/database research cannot.

## 2. Research Source Gateway

Research Agent does not directly create authoritative evidence from arbitrary model-visible search output.

All formal evidence must pass through Research Source Gateway:

```text
Research Agent
  -> Research Source Gateway
      -> structured connectors
      -> real web search
      -> browser navigation
      -> page/PDF retrieval
      -> evidence normalization
  -> Evidence Ledger
```

## 3. Capabilities

### Structured connectors
Initial examples:
- Crossref
- OpenAlex
- PubChem
- selected materials databases
- patent/public standard sources
- additional public APIs

Do not hardcode these as the only future connectors.

### Web search
At least one real configurable provider must be implemented.

### Browser-level research
Use a real headless browser capability (recommended Playwright) for:
- dynamic pages
- supplementary files
- nested navigation
- pages without API
- verifying original sources

DSH built-in web search/fetch may be used as an underlying capability **only if RAVEL still enforces provenance registration and source validation**. Do not expose an untracked bypass to Research Agent.

## 4. Search result is not evidence

Flow:

`search result → lead → open original source → verify → register evidence`

Snippet/AI search summary cannot directly satisfy Acceptance Criteria provenance.

## 5. Model prior

LLM prior knowledge may:
- suggest query
- explain concepts
- propose hypothesis
- guide search

It may not:
- enter Evidence Ledger without verification
- establish a numerical benchmark
- be cited as provenance

## 6. Source tiers

### Tier A
- peer-reviewed primary literature
- official standards
- authoritative scientific databases
- original experiment/computation data

### Tier B
- preprints
- patents
- official technical reports
- government/university/research-institute sources

### Tier C
- vendor technical pages
- product specifications
- industry associations

### Tier D
- ordinary websites
- news
- blogs
- forums

Real lower-tier sources may be useful but must remain visibly lower confidence.

## 7. Evidence Sufficiency

Critical decisions require an assessment, not a mechanical paper count.

Consider:
- independence
- authority
- directness
- condition match
- reproducibility
- conflict

Suggested categories:
- STRONG
- MODERATE
- WEAK
- INSUFFICIENT

## 8. Fact / Inference / Hypothesis

Every Research Record separates:

### FACT
Directly supported by verified source.

### INFERENCE
Reasoning derived from facts. Must include reasoning and evidence refs.

### HYPOTHESIS
Project-specific proposition requiring validation.

## 9. Conflicts

Conflicting evidence is preserved, not averaged away.

Evidence records can include:
- conflicts_with
- conflict_reason
- condition_difference
- unresolved status

## 10. Snapshot/reproducibility

For important sources store:
- original URL/DOI/database ID
- title/authors/date
- retrieved_at
- content hash
- exact relevant excerpt/data pointer
- normalized text
- source tier
- access status

If legally/technically allowed:
- PDF
- supplementary file
- webpage snapshot

If inaccessible:
- PAYWALLED
- AUTH_REQUIRED
- ACCESS_LIMITED
- POLICY_BLOCKED

Never invent missing text.

What the implementation stores is narrower than the list above in one respect, and
it is worth knowing before relying on it: the readable text an *opening* returns
is a verbatim excerpt of at most 600 characters, produced only for HTML. The
snapshot behind the row is the whole thing, though, and since Phase 11 the
Research seat can read it again.

The reading surface is three tools, all of them reads, all of them Research's:

- **`source_metadata`** — what the ledger says about a source, whether its bytes
  are available, and what kind of document they are. Returns no text, so a
  session can decide whether to spend a read without spending one.
- **`read_source`** — a bounded region: `start`/`length` in characters of the
  document's text, or `pages` ("3", "3-5", "1,4,9-11") for a PDF. The result
  carries where the region began and ended, how much there is in total, and the
  arguments that read the next region.
- **`search_source`** — a literal, case-insensitive search *inside* a source
  already in hand, returning offsets into the same text `read_source` returns.
  It is not a web search: nothing here establishes that a source exists or that
  what it says is true.

Reading is bounded in both directions. One read returns at most 24,000
characters, a PDF at most five pages at a time, and a search walks at most
forty pages of a PDF unless a range is named. The bound is on what the call
costs as well as on what it returns: a PDF read extracts the pages it was asked
for and no others.

Formats: HTML, plain text, JSON and XML are read as their text, with a `note`
saying what was removed (tags, comments, whether a re-indent happened) so the
source's words are distinguishable from RAVEL's rendering of them; a PDF is
read page by page; anything else is reported as a format with no words in it
rather than returned as empty.

**What it will not do is more important than what it does.** A scanned PDF —
pages, no text layer — comes back with empty pages and a sentence saying that
reading words out of images is OCR and RAVEL does not do it. An encrypted PDF
says it is encrypted. A format with no text says so. A document that does not
parse is reported as a fact about the source. Nothing in the path can produce
text the source did not contain, which is the only property that makes the
reading worth citing.

Provenance stays where it was. The tools write nothing — no row, no counter, no
artifact — and the `source` block they return *is* the ledger row: `source_id`,
`content_hash`, `retrieved_at`, `snapshot_ref`, access status and tier. The
bytes are read from the snapshot and hashed again before anybody sees them; if
they do not hash to what the row records, the read is refused and the refusal
names both hashes. A PDF's text is the paper's own, not a landing page and not a
snippet: `search result/snippet is a lead` still holds, and what makes something
evidence is that RAVEL opened and read it.

Computation-methodology research — a functional, a force field, a charge method,
a pseudopotential, a convergence criterion, a published parameter set — is
reached the same way as anything else, out of the same ledger, with the same
provenance. What a Research seat may do with it is establish and cite it;
choosing which method a computation runs under is a scientific decision, and
that is Master's.

A deployment with no object store can read the ledger's metadata and not its
bytes: the tools say which of the two is missing rather than failing. See
`KNOWN_LIMITATIONS.md` L-25 for the history and the residual gap.

## 11. Research Completion Contract

A RESEARCH node cannot be COMPLETE unless:
- core questions covered
- key factual claims have provenance
- Evidence Sufficiency assessed
- conflicts recorded
- Fact/Inference/Hypothesis separated
- unknowns explicitly declared
- suggested acceptance criteria have provenance/status
- recommended followups provided
- structured ResearchRecord validated

If not met:
- continue research
- or return INCOMPLETE with explicit gaps

## 12. Dual output

Authoritative:
- Structured ResearchRecord

Projection:
- Human-readable Research Report

Master should consume structured record first and retrieve long report only as needed.

## 13. Reading a Result Back

The chain the design claims is `Master → Research Agent → Evidence → Master →
Decision`, and until Phase 11 the last arrow had no tool behind it. Master could
read the whole project state and could not read one research task's result, so
the only way to act on what a task found was to be the session it was produced
in — which a replacement session is not.

Four tools close it. All four are reads, all four are Master's alone, and none
of them appears in `registry.WRITE_TOOLS`:

- **`list_research_results`** — every research task in the project, with the
  record it handed over (if any), the completion status and sufficiency
  assessment it was judged against, how many claims, sources and conflicts its
  ledger holds, and the verdict Review gave it. Counts, not contents: the
  listing answers *which* task is worth reading, not what it says.
- **`read_research_result(node_id)`** — the Research Record as the seat
  submitted it, the Evidence claims it was assembled from, those claims'
  source rows, the recorded conflicts, a sufficiency assessment measured live
  from the ledger, and the verdicts given about the node. `result` is `null`
  for a task that has not submitted one, and the live assessment is still
  there, so a task under way is readable rather than blank.
- **`read_evidence(evidence_id)`** — one claim, its sources, and the conflicts
  it is part of, for when the wording of a single claim is what a decision
  turns on.
- **`read_source_metadata(source_id)`** — one source's ledger row: requested
  and final URL, title, DOI, media type, retrieval time, content hash, tier,
  access status, whether a snapshot exists, and which claims cite it. **No
  text.** It is the same row §10's `source_metadata` returns to Research, read
  by a different seat and named differently on purpose: two tools of one name
  serving two roles would be one roster entry with two meanings. What a
  document says is read by the seat whose task it answers, and a
  passage worth quoting is worth a research task that quotes it into the
  ledger; returning the text here would make Master a second reader of the
  document rather than a reader of the record.

`read_project_state` carries `completed_research`: the research tasks that have
handed a result over, one flat summary each — the same shape the listing
returns, so the two cannot drift. It is a pointer, not the evidence. A task
still being worked on is deliberately absent (`list_research_results` names
it), and no claim text reaches the state read, which is the read every turn
begins with.

What stays separate is authorship. The ledger and the record are Research's;
Master reads them and may not write to them, which is enforced by the roster
rather than by the prompt — no Master-facing tool can register a claim, a
source, or a record. Where Master needs a claim that Research did not record,
the answer is a research task, not a line to add.
