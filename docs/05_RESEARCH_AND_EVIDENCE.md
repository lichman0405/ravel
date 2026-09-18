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
