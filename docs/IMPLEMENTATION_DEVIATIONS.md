# Implementation Deviations

Development agent must append here if implementation needs to diverge from specification.

Each entry:

- Date
- Spec section
- Original requirement
- Actual implementation
- Reason
- Risk
- Whether acceptance behavior remains equivalent
- Follow-up

---

## D-001 — One DSH runtime process per `(project_id, role)` instead of one DSH Host

- **Date:** 2026-09-19
- **Spec section:** `CLAUDE.md` ("单台 CVM 的 V0 可以运行一个 DSH Host，并承载多个 Project/session");
  `docs/07_DSH_INTEGRATION.md`; `docs/13_REPOSITORY_TARGET_STRUCTURE.md` (`dsh/agent-presets/ravel-*`)
- **Original requirement:** One DSH Host carries multiple Projects and sessions, with agent role
  selected per session through an agent preset.
- **Actual implementation:** RAVEL launches one DSH runtime process per `(project_id, role)` pair,
  lazily on first use and idle-reaped afterwards. Role separation comes from the profile each
  process is launched with, not from a per-session preset.
- **Reason:** Two properties of the pinned release, both verified directly against
  `183f08e9c6dde7e36cd2318eaee70b0da08fb35e`:
  1. The SDK JSON-RPC server cannot join an agent preset. `packages/sdk/server/src/server.ts`
     `createSession()` composes an agent with no `setup` hook, and `AgentPresets.mount()` runs only
     inside one. The shipped `sdk-app` and `sdk-minimal` bundles do not even depend on
     `dsh-agent-presets`. Per-session preset selection is therefore unreachable through the SDK.
  2. An MCP server cannot identify its caller. `packages/mcp/mcp-client/src/tools.ts:89` sends
     `{ method: 'tools/call', params: { name, arguments } }` — no session id. A single shared MCP
     server would have to accept a model-supplied project id, which
     `docs/09_SECURITY_AND_IDENTITY.md` forbids ("project-scoped authorization > model-supplied
     project_id").
  Scoping the process lets `RAVEL_PROJECT_ID` and `RAVEL_ROLE` reach the MCP server through the
  environment, which `dsh-mcp-client` merges over the scrubbed ambient env. Authority becomes
  structural rather than asserted.
- **Risk:** More runtime processes than a single-host design. Bounded by active project count times
  active roles, mitigated by lazy start and idle reaping. Role isolation is stronger than the
  original design, not weaker.
- **Acceptance behavior:** Equivalent or stronger. A02, A15, and A19 are unaffected; A19 (DAG
  authorization) is enforced twice — once by profile composition, once by the mutation service.
- **Follow-up:** Re-evaluated at the next public tag (`dsh-v0.1.6-alpha.2`) on 2026-09-20. The
  blockers persist: the Python SDK still exposes only `initialize` / `session/prompt` / `shutdown`;
  `packages/sdk/server/src/server.ts:274-292` still creates sessions without joining an Agent Preset;
  and `packages/mcp/mcp-client/src/tools.ts:138-141` still forwards tool calls without session/project
  identity. The `alpha.2` tag has no release assets or PyPI wheels. The deviation therefore remains
  in force. Re-evaluate again only when a future DSH release exposes preset selection or
  session-scoped MCP servers through the Python SDK.

## D-002 — Role capabilities are exposed as Python MCP servers, not DSH-native tools

- **Date:** 2026-09-19
- **Spec section:** `docs/13_REPOSITORY_TARGET_STRUCTURE.md` ("DSH-native bundle/preset layer may use
  JavaScript/TypeScript/Cordis config")
- **Original requirement:** Allow the minimum DSH-native JS/TS code needed to register tools or host
  services.
- **Actual implementation:** No TypeScript is written at all. Every RAVEL tool is a Python MCP
  server over stdio, mounted with a `@deepseek-ai/dsh-mcp-client` row.
- **Reason:** The pinned SDK protocol has no tool-registration surface in either direction
  (`initialize` / `session/prompt` / `shutdown`; server-to-client requests have no producer). The
  MCP client row is configuration-only and is the officially supported external-tool seam at this
  pin. `docs/13` explicitly prefers the simpler supported mechanism after validating the pinned
  version, and `docs/15` prefers official mechanisms over new DSH-native code. This keeps all
  authorization and domain logic in Python, where the authoritative state lives.
- **Risk:** Tool calls cross a process boundary, adding latency and a serialization format to
  maintain.
- **Acceptance behavior:** Equivalent. Every tool call is authorized inside the RAVEL MCP server
  against the session binding and the project scope.
- **Follow-up:** None unless a future pin provides a same-process Python tool seam.

## D-003 — Research Agents receive no DSH built-in web tool

- **Date:** 2026-09-19
- **Spec section:** `docs/05_RESEARCH_AND_EVIDENCE.md`; `START_PROMPT.md` §3 "Real research"
- **Original requirement:** Research must be real; a search result is only a lead until the
  original source is opened; Evidence records metadata, `retrieved_at`, hash, and quoted content.
- **Actual implementation:** Research role profiles mount only RAVEL's research MCP server.
  `@deepseek-ai/dsh-tool-web` is not mounted.
- **Reason:** A page fetched by a DSH built-in tool is invisible to RAVEL — no retrieval timestamp,
  no content hash, no snapshot, nothing the Evidence Ledger can cite. Routing retrieval through the
  gateway is what makes provenance recordable at the moment of retrieval.
- **Risk:** Research depends on RAVEL's gateway being available, and cannot fall back to the
  harness's own fetch.
- **Acceptance behavior:** Stronger. A03 and A04 require registered Evidence to carry verifiable
  provenance, which the built-in tool could not supply.
- **Follow-up:** None.

## D-004 — The address guard allows the benchmarking range, and pairs it with a name check

- **Date:** 2026-09-19
- **Spec section:** `docs/09_SECURITY_AND_IDENTITY.md`; `docs/05_RESEARCH_AND_EVIDENCE.md`
- **Original requirement:** Research retrieval must reach real sources, and must not become a way
  for a model-supplied URL to make requests inside the network RAVEL runs in.
- **Actual implementation:** `src/ravel/research/addressing.py` refuses any URL whose scheme is not
  http/https, whose host is one of the well-known instance-metadata names, or whose resolved
  addresses are not public — checked at the httpx transport so redirect hops are checked too, and
  again before every browser navigation. The blocklist covers loopback, link-local, RFC 1918,
  carrier-grade NAT, unique-local, unspecified, multicast and reserved space. It deliberately does
  **not** cover `198.18.0.0/15`, nor the documentation ranges.
- **Reason:** The deployment host intercepts DNS: every name answers with a `198.18.56.x`
  placeholder (RFC 2544 benchmarking space, the usual choice for this) and the interceptor forwards
  the connection itself. Verified on the host — `api.crossref.org`, `arxiv.org`, `doi.org` and
  twelve other real sources all resolve into that range, and all of them answer HTTP 200 through
  it. Blocking the range would leave RAVEL unable to reach the open Internet *while protecting
  nothing*, because a connection to a placeholder address is answered by the interceptor rather
  than by any internal service. The same property breaks the address check in the other direction:
  `metadata.google.internal` also resolves to a placeholder, so the address cannot distinguish it
  from a journal. The name check is what covers that case, and it is the reason both checks exist.
- **Risk:** On a host whose DNS is intercepted, a hostname the attacker controls that resolves to
  the metadata service through the interceptor's own resolver is not caught by the address check,
  and is caught by the name check only if it uses a well-known metadata name. `SECURITY_NOTES.md`
  and `KNOWN_LIMITATIONS.md` record this as a property of the environment, with the deployment
  assumption (a host that resolves names honestly) stated explicitly.
- **Acceptance behavior:** Equivalent. A05–A08 concern what is recorded about a source that was
  read, which is unchanged; the guard only decides which URLs reach the fetching layer at all.
- **Follow-up:** If RAVEL is later deployed on a host without DNS interception, re-check that the
  allowed ranges are still the intended set; a deployment-time probe would make the assumption
  visible rather than documented.
