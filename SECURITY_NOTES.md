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

### DNS rebinding: the connection is pinned to the validated address

The guard resolves a hostname, and the HTTP client used to resolve it a second
time when it connected. An authoritative server that answered publicly for the
first query and `169.254.169.254` for the second won that race, and the check
had validated an address the request never went to.

**Fixed for the fetcher.** `addressing.address_for` returns the addresses it
validated, and `_PinnedBackend` (`ravel.research.fetching`) is the network
backend the HTTP pool is built with: it calls `address_for` and hands the
resulting **address** to the socket layer. The name is not lost — httpcore still
knows the host and port the request is for, and uses them for the `Host` header,
for the TLS server name, and for certificate verification — so the request that
goes out is identical to what it would have been, and only the address it went
to was chosen by the guard rather than by the resolver. There is now exactly one
DNS lookup in the path, and its answer is the one that was checked.

Two consequences worth naming:

- **Keep-alive is off** (`max_keepalive_connections=0`). A pooled connection is
  keyed by the address it was opened to, so two names behind one address would
  share a connection whose TLS session was established for the first of them.
  RAVEL fetches a handful of URLs per call; a handshake each is cheaper than
  reasoning about that.
- **`httpx.HTTPTransport` is no longer used**, because it builds its own
  network backend and does not accept one. The transport in `fetching.py` is
  built on `httpcore.ConnectionPool` directly and mirrors what `HTTPTransport`
  does with the response. `tests/live_research` re-verified real Crossref,
  OpenAlex, arXiv and publisher TLS against it — that is what `d647084` records,
  against the 11 cases the suite held then. The suite now holds 13 cases and
  passes on this host once `RAVEL_RESEARCH_CONTACT_EMAIL` is set; without it the
  suite still skips in full rather than fetching anonymously. `KNOWN_LIMITATIONS.md`
  L-20 records what that costs and how little it takes to close.

**Not fixed for the browser.** Playwright's connections are made inside a
browser process this code does not own and cannot give a network backend to.
`BrowserNavigator` guards the URL it is given and every request the page makes
through a route handler, and refuses the ones it can see — but a name that
answers differently on the browser's second lookup still reaches the second
answer. The name check still catches the well-known metadata names regardless of
what they resolve to, which is the highest-value target, and Chromium is given
no proxy and no credentials. Recorded as **L-14** in `KNOWN_LIMITATIONS.md`.

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

## 4. Authority is created in a few places, and all of them now check

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

### The same audit, run again on the review package

A later review asked the same question of the code written since, and found one
door: **a Worker could write its own PASS.** `ReviewService.submit` took an
`actor_id` and defaulted it to `AgentRole.REVIEW.value` — it *recorded* Review as
the author rather than checking that Review was the caller — so a Compute Worker
that could reach the service could accept its own node's work under Review's
name.

The check now sits in `ReviewRepository.submit`, at the write, and takes the
caller's role as a required keyword. `DecisionRepository.record` had the same
shape and is fixed the same way, and both go through one function,
`ravel.domain.roles.require_role`, which is also what `require_master` delegates
to: one implementation, so the several call sites cannot disagree about what the
rule says. The role is the one RAVEL bound to the caller's scope, never a name in
a payload — which is why the function takes a value rather than looking one up.

Tests: `tests/integration/review/test_pre_run_gate.py::test_only_review_may_submit_a_verdict`
(a parametrized refusal for the three other agent roles, asserting nothing was
written) and `::test_the_refused_worker_does_not_open_the_gate`.

### A check that was written down but not on the path

The same review found the pre-flight review gate **fail-open**.
`pre_run_clearance` and `NotClearedError` existed, were documented as *the*
checkpoint that clears a node to run — `acceptance/V0_ACCEPTANCE.md` A09 requires
it — and had no production caller at all. Nothing on the path into `RUNNING`
consulted them, so a COMPUTATION or EXPERIMENT node could be handed to a Worker
with no review, or after a refusal, and the only thing preventing it was that
nobody had written the code to skip the review.

The gate now lives in `DagNode.can_enter_running`, which is the one place every
route into RUNNING passes through: a Worker's activity, Master's own transition,
a test's fixture. It refuses when the node's latest PRE_RUN verdict is absent,
FAIL, or PARTIAL, and the parameter that carries the verdict defaults to `None`,
which is a refusal — so a caller that forgets to pass it fails closed rather than
open. `pre_run_clearance` was kept, and its docstring now says what it is: the
readable form of a rule enforced elsewhere, for callers that want to explain a
wait rather than to permit a run.

The evidence the gate is on the real path is that wiring it broke 26 tests, in
exactly the places that build a runnable node. Four fixtures — the shared
`prepare`, the temporal `runnable_node`, the review package's `computation` and
`Driving`, and the DAG package's new `clear_to_run` — now pass the checkpoint
the way the runtime does, through `ReviewService` rather than by writing a row.

Tests: `tests/integration/review/test_pre_run_gate.py`, whose gate tests assert
the *transition* refusing rather than a function agreeing that it would, plus
`::test_the_enforced_gate_and_the_readable_one_agree`, which compares the SQL
that decides against the Python that explains for the same node — the two
implementations of one rule nothing else makes agree.

### A guard on the wrong side of the write

Writing A12's tests turned up the same family a third time, in the append-only
triggers. `DeviationRepository.resolve` writes `resolved_by_decision_ref` and
`resolved_at` onto an escalation; `deviation_records` was not in
`UPDATABLE_TABLES`, so the trigger refused **every** resolution. The method had
a docstring describing what it did, a check constraint
(`resolution_is_all_or_nothing`) making the write all-or-nothing, and no caller
that had ever run — so the mistake was invisible for exactly as long as nothing
asked Master to answer a Worker.

Two things made it invisible, and both are worth recording. The first is that
the tests which would have called it were failing earlier for an unrelated
reason, so the traceback pointed at the trigger rather than at the missing list
entry. The second is that the API-level checks *looked* complete: the
constraint that keeps the two resolution columns consistent existed and was
asserted, and it is easy to read an enforced constraint as evidence that the
write it constrains is permitted.

The table is now updatable, and narrowed in the same move: a new identity
trigger (`ravel_deviation_records_identity`) fixes which node raised the
escalation, what was asked for, why the contract refused, and when. Moving the
table into the updatable set without it would have made the whole row
rewritable, and a deviation whose `requested_action` can be edited after Master
answered it is a record that can be made to agree with any decision taken.
`tests/integration/state/test_guards.py` asserts the update is refused when it
touches anything but the two resolution columns.

### The one place authority is created without a check, and why it is a terminal

The rules above all assume there is already an authority to check against. Some
path has to create the first one, and in V0 that path is
`scripts/create_account.py` — an operator command run on the host, not a route.
`MembershipRepository.grant` permits a project's first membership with no
granter because the alternative is a project nobody can ever direct; every
membership after it requires a granter holding at least what is conferred.

That exception is safe for a reason worth stating rather than assuming: the
script is not reachable over the network at all. It imports the domain
repositories and calls them in-process, so it needs the deployment's database
credentials to do anything — which are the same credentials that would let a
holder write the rows directly. It grants no capability that reaching the
database does not already grant, and the Gateway, whose credentials are the ones
a person can obtain by logging in, has no route that creates an account or a
membership.

What the script deliberately does *not* do is bypass the domain. Every write
goes through the same repositories the runtime uses, so the granter check, the
active-account check, the append-only guards and the check constraints all
apply to an operator exactly as they apply to Master. It also refuses to call
`sys.exit()` inside a transaction: `Database.transaction()` catches `Exception`
and not `SystemExit`, so an exit taken mid-transaction would skip the explicit
rollback and leave the context manager to discover it in its `finally`. Every
refusal is raised and reported after the transaction has closed.

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

Since Phase 11, a PDF's bytes are parsed too — by `pypdf`, in
`ravel/research/deepread.py`, when the Research seat reads back a source it
already registered. Three things bound it. The bytes are the ones RAVEL stored
and hashed, not bytes fetched at read time, so a document is parsed only after
the ledger has a `content_hash` for it. The module is a pure function of those
bytes: it holds no client, resolves nothing, and cannot reach the network from a
PDF's contents — an embedded link or an external stream reference is text in the
document, not a fetch. And every failure is caught and reported as a property of
the document (unparseable, encrypted, page not extractable) rather than
propagated, because a malformed paper arrives as a traceback otherwise, which is
both a worse answer and a worse failure mode. Nothing extracted is executed:
the text goes back to the model as data, and the HTML/XML path still strips
markup rather than rendering it.

---

## 7. What the automated reviews found

Both automated reviews of the research package ran against source, and both
findings they produced were real:

| Finding | Disposition |
|---|---|
| SSRF: the browser guarded only the URL it was given, not redirects or subresources | **Fixed.** Route handler on `**/*`; service workers blocked; aborted navigations re-raised as `UnsafeURL`. |
| Evidence integrity: a retrieval's hash could be recorded unverified on the no-store path | **Fixed.** `register` recomputes it from the body. |
| SSRF: DNS rebinding TOCTOU between the check and the connection | **Fixed for the fetcher**, by pinning the connection to the validated address (§1). **Open for the browser**, which cannot be given a network backend; recorded as L-14. |
| Fail-open review gate: nothing on the path into RUNNING called `pre_run_clearance` | **Fixed.** The gate is in `DagNode.can_enter_running`, on the transition, and fails closed on a missing argument (§4). |
| A Worker could submit a Review Record and be recorded as Review | **Fixed.** `require_role` at the write in `ReviewRepository.submit`, and the caller's role is a required keyword (§4). |
| A fourth finding | **Not retrieved.** The review notification body arrived truncated and the full report is not on disk; what is recorded here is what could be read. |

Two of the three earlier access-control findings were likewise fixed in place
rather than described: the gateway's write path is scoped by construction (the
project id comes from the gateway, not the caller) and is covered by
`tests/integration/research/test_gateway_registration.py::test_a_gateway_cannot_write_into_another_project`.

A third finding from the same review — a migration that creates a table without
re-installing the guards would ship unguarded, and the parity gate between the
migrated schema and the model-built one compared tables, columns, and check
constraints but **not triggers** — was real as a gap in coverage rather than as
a live hole: every table is covered today, because `guards.install()` is called
from the initial migration and again from the revision that adds `backend_jobs`.
It is now checked rather than relied on:
`tests/integration/state/test_migrations_match_the_models.py::test_the_chain_installs_the_guards_the_suite_asserts_against`
compares the guard triggers table by table between the two schemas, in both
directions, and `::test_the_chain_installs_every_guard_function` checks the
functions they call.

---

## 8. Where the remaining risk sits

Ranked by what an attacker gains, with the entry that covers each:

1. **DNS rebinding inside the browser** (§1) — needs control of authoritative
   DNS for a host a page navigates to or subresources, and a won race the route
   handler cannot see. Open for the browser only; the fetcher is pinned.
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
