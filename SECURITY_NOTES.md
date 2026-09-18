# Security Notes

What RAVEL V0 does about the ways it could be made to do something it should
not, what it does not do, and what that costs. Written for the person who has to
decide whether to run this on a host that matters.

The pattern in every section is the same: state the failure concretely, state
where the check sits, and state what the check does not cover. A security
document that only lists defences is a document that hides the attack it has
not thought about.

---

## 1. The research gateway fetches URLs RAVEL did not choose

A lead comes from a search provider, a link comes from a page, a URL is
assembled by a model. Every one of those is a string that arrived from outside,
and handing it to an HTTP client is a request made on behalf of whoever wrote
it, from inside the network RAVEL runs in. On a cloud host that network contains
an instance metadata service; on any host it contains PostgreSQL, MinIO, and
Temporal.

**Where the check sits.** `ravel.research.addressing.guard` refuses a URL whose
scheme is not http/https, whose host names the instance metadata service, or
whose host resolves to an address that is not on the public internet. It is
called from three places, and the placement is the design:

| Path | Hook | Why there |
|---|---|---|
| `Fetcher` | an `httpx.BaseTransport` wrapper | A transport sees every hop, including the ones a redirect added. A check in front of the client would validate the URL RAVEL wrote and miss the one the server chose. |
| `BrowserNavigator.open` / `links` | first statement, calling thread | The answer does not depend on the browser, so the refusal happens before a browser is started — which also means it still works when Chromium cannot start. |
| `BrowserNavigator` page | a `context.route("**/*")` handler | A page's own requests — an `<img>`, a script, an XHR, a `302` — are chosen by the page. Chromium issues them itself, so without this hook RAVEL is the client for a request nobody wrote. |

**Service workers are blocked, not guarded.** Requests a service worker makes
do not pass through Playwright's route interception, so a page that registers
one has a path the route handler never sees. `new_context(service_workers="block")`
closes it. RAVEL reads documents; it has no use for an offline cache, and a page
that needs one to show its content is a page to read another way.

**A refused navigation is reported as a refusal.** An aborted navigation
surfaces from Playwright as a generic failure. Left that way it would be
recorded downstream as "this source could not be read" — a different and untrue
statement about a URL RAVEL declined to request, and precisely the ledger entry
an attacker probing the internal network is trying to obtain. `_Session.goto`
re-raises the `UnsafeURL`. Refusals raise out of the gateway rather than being
recorded as restricted sources, for the same reason.

### What this does not stop: DNS rebinding

The guard resolves a hostname and then the HTTP client resolves it again when it
connects. An authoritative server that answers publicly for the first query and
`169.254.169.254` for the second wins that race. **This is not fixed.** Closing
it means connecting to the address the guard validated rather than to the name,
which changes how the client connects (URL rewritten to the literal address,
`Host` header preserved, TLS SNI and certificate verification pinned to the
hostname) rather than adding a check in front of it.

What bounds it today:

- The name check catches the well-known metadata names regardless of what they
  resolve to, which covers the highest-value target.
- The attacker must control authoritative DNS for a host RAVEL fetches, and must
  win a race between two lookups.
- RAVEL's fetches are GETs and HEADs. A request that reaches an internal service
  can still be a state-changing GET on a badly built one.

**Follow-up:** pin the connection to the validated address, and re-verify the
live suite against real research hosts afterwards.

### The trade-off this policy makes on hosts that intercept DNS

`_BLOCKED_NETWORKS` omits `198.18.0.0/15` and the documentation ranges
(`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`). No
service listens on the documentation ranges, so refusing them protects nothing.
`198.18.0.0/15` is the benchmarking range most commonly used as a DNS
placeholder by software that intercepts name resolution and forwards the
connection itself — a real deployment, and the one this project is developed on.
Refusing it would make RAVEL unable to reach the open Internet there while
protecting nothing, because a connection to that range is answered by the
interceptor rather than by anything internal.

The cost: on such a host the address check can no longer distinguish the
metadata service from a journal by address, and a hostname the attacker controls
that the interceptor resolves to the metadata service is caught only by the name
check. This is recorded as deviation D-004 in `IMPLEMENTATION_DEVIATIONS.md` and
in `KNOWN_LIMITATIONS.md`. It is a deliberate trade, not an oversight, and it is
the reason the name check exists at all.

---

## 2. Evidence integrity: a hash that describes bytes RAVEL holds

`EvidenceSource.content_hash` is what makes a citation checkable. Three checks
keep it meaningful, and each covers a case the others cannot:

1. **`Retrieval` refuses a hash with no body**, and refuses a restricted
   retrieval that carries a hash or an excerpt. A paywalled source with a
   content hash would assert that RAVEL read bytes it could not reach.
2. **`register` recomputes the hash from the body** and refuses a mismatch. This
   is the check that covers the no-store path: with no snapshot written, the
   retrieval's own hash was recorded on the strength of the retrieval saying so,
   and `register` accepts any `Retrieval` — including one assembled from a tool
   result.
3. **A written snapshot is compared with the retrieval.** The store hashes the
   object it wrote; a difference means what reached storage is not what was
   read. This is what makes `verify` more than a comparison with itself.

The three together give one property: an `EvidenceSource` hash is one RAVEL
computed over bytes it has, whether or not a snapshot was kept.

---

## 3. Credentials and the harness process

### What is scrubbed

DeepSeek Harness spawns MCP server children from `scrubbedParentEnv()`
(`packages/subprocess/subprocess/src/index.ts` at the pinned commit), which drops
every environment name matching `/KEY|PASSWORD|SECRET|TOKEN/i` and every `DSH_*`
name, then merges the explicitly configured `env` block on top. RAVEL's tool
server receives exactly `RAVEL_PROJECT_ID`, `RAVEL_ROLE`, and `RAVEL_BRIEF_FILE`
over that scrubbed base. `RAVEL_DEEPSEEK_API_KEY`, `RAVEL_SEARCH_API_KEY`,
`RAVEL_S3_SECRET_KEY`, and `RAVEL_POSTGRES_PASSWORD` are all dropped by the
pattern.

### What is not

**`RAVEL_POSTGRES_DSN` survives the scrub.** The name contains none of the
four words, and `.env.example` documents a value with the password inline:
`postgresql+psycopg://ravel:ravel_dev_password@127.0.0.1:55432/ravel`. Any
process that reads that environment has the database credential.

**The harness process itself inherits RAVEL's whole environment.** The Python
SDK builds the child environment with `env = os.environ.copy()` before merging
`config.env` (`deepseek_harness/client.py:76`), so there is no subtraction:
whatever RAVEL's process holds is held by the `dsh` process too. That includes
the DSN above, and every credential RAVEL itself reads.

**What bounds it:** the agent has no shell in that process. RAVEL disables
`persistent-bash` and `persistent-pwsh` in every role profile, so the model
cannot read the environment it runs beside. The exposure is to the harness
binary, to any DSH feature that reads the environment, and to anything that
dumps a process's environment — a crash reporter, a `/proc` read by another
unprivileged user on a shared host.

**Follow-up:** either launch the harness with an explicit minimal environment
(needs an SDK change, since `config.env` merges rather than replaces), or stop
using an inline-password DSN so that the component variables — which the scrub
does match — are the only form the credential takes. The second is available
today and needs no upstream change.

---

## 4. Authority is created in two places, and both now check

`docs/09_SECURITY_AND_IDENTITY.md` separates Execution, Review, and Decision.
The audit that produced the fixes below asked a narrower question than "is the
separation right": *where can authority be created without going through the
method that grants it?* Four doors were open beside the locked ones, and each is
now closed where the write happens, with a test that fails if the check is
removed (`2b40d32`):

- **An agent could answer its own approval request.** `ApprovalRepository.resolve`
  took `resolved_by` as given, so writing down the name of a human who never saw
  the request produced a record saying the project owner approved. The identifier
  is now checked against a membership in that project whose role satisfies the
  request's `required_role`, on an active account.
- **Anyone could grant anything.** `MembershipRepository.grant` now requires a
  granter holding at least the authority being conferred. Only a project's
  *first* membership may be created without one, since otherwise a caller could
  reach power through an empty project.
- **Any role could ask a human for authorization.** Asking is part of deciding to
  act, so `ApprovalRepository.request` takes the caller's role and refuses
  anything but Master.
- **Nothing could compare a held role against a required one.**
  `ProjectMembership.satisfies` now ranks `LAB_USER < PROJECT_OWNER < ADMIN`.
  Admin outranking owner is deliberately narrow — it lets an administrator answer
  a request waiting on a human — and no method anywhere takes a user identity and
  writes a DAG node, so it cannot become a scientific decision.

## 5. Tool authorization

The model never supplies a project id. A tool call is authorized against the
scope RAVEL launched the server with (`RAVEL_PROJECT_ID`, `RAVEL_ROLE`), read
once at startup from the environment, never from tool arguments
(`ravel/mcp/scope.py`, `ravel/mcp/context.py`). A project id arriving in an
argument is not used for authorization even when it agrees with the scope.

The runtime brief is validated on load: unknown roles, unknown bindings, and
malformed JSON are refusals rather than defaults.

The research gateway follows the same rule at its own boundary: the project id
comes from the gateway's construction and never from the caller, so a gateway
cannot be asked to write into another project
(`tests/integration/research/test_gateway_registration.py::test_a_gateway_cannot_write_into_another_project`).

---

## 6. Untrusted input parsing

The arXiv connector parses XML that arrived over the network with
`resolve_entities=False` and no network access, which is what stops entity
expansion and external-entity fetches (`ravel/research/connectors/arxiv.py`,
`_PARSER`). HTML is parsed with lxml/BeautifulSoup for text extraction only;
nothing RAVEL extracts is executed or evaluated.

URLs extracted from a page are filtered by `_absolute` before a research task
ever sees them: non-http schemes are dropped rather than returned, so the
caller cannot be handed a `javascript:` link to open.

---

## 7. What the automated reviews found

Both automated reviews of the research package ran against source, and both
findings they produced were real:

| Finding | Disposition |
|---|---|
| SSRF: the browser guarded only the URL it was given, not redirects or subresources | **Fixed.** Route handler on `**/*`; service workers blocked; aborted navigations re-raised as `UnsafeURL`. |
| Evidence integrity: a retrieval's hash could be recorded unverified on the no-store path | **Fixed.** `register` recomputes it from the body. |
| SSRF: DNS rebinding TOCTOU between the check and the connection | **Open, documented** in §1 and `KNOWN_LIMITATIONS.md`. |
| A fourth finding | **Not retrieved.** The review notification body arrived truncated and the full report is not on disk; what is recorded here is what could be read. |

Two of the three earlier access-control findings were likewise fixed in place
rather than described: the gateway's write path is scoped by construction (the
project id comes from the gateway, not the caller) and is covered by
`tests/integration/research/test_gateway_registration.py::test_a_gateway_cannot_write_into_another_project`.

---

## 8. Where the remaining risk sits

Ranked by what an attacker gains, with the entry that covers each:

1. **DNS rebinding to the metadata service** (§1) — needs control of
   authoritative DNS for a fetched host and a won race. Open.
2. **A credential in a child process's environment** (§3) — needs local access
   to the harness process or a DSH feature that reads the environment. Bounded
   by the agent having no shell.
3. **The interpreter-range trade-off on a host that intercepts DNS** (§1) — an
   address check that cannot see the difference, with the name check as the
   remaining signal. Deliberate, recorded as D-004.

Everything else in this document is a check that fails closed: a URL that cannot
be validated is refused, a hash that does not describe its bytes is refused, an
approval with no qualified human behind it is refused, and a scope that cannot
be read is an error rather than a default.
