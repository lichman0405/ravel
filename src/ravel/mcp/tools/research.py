"""Research's surface: the way in to the Evidence Ledger, and the way out.

`docs/05` gives the flow as *search result → lead → open the original source →
verify → register evidence*, and every step of it is a tool here. That is the
whole design: the ledger has one entrance, this module is it, and there is no
handler that writes a source row from anything other than a retrieval the
gateway produced.

The flow has two ends as well as a middle. `begin_research` is the way a task
starts — the Research seat's own act, the counterpart of a Worker's
`start_execution` — and `submit_research_record` is the hand-over: the record
is written and the node goes to REVIEWING in one transaction, where the FINAL
checkpoint judges it.

Three properties the handlers enforce rather than describe:

- **A lead is not evidence and cannot become any.** `search_sources` and
  `search_web` return leads. There is no parameter anywhere in this module that
  accepts a URL and a hash from the caller; `register_source` names a retrieval
  this process opened, and `register` itself recomputes every hash from the
  bytes it holds.
- **A claim's tier is derived, not chosen.** `record_evidence` takes the
  sources a claim rests on and reads their tiers out of the ledger. A model
  that could name its own tier could file a vendor page as tier A, and the
  sufficiency assessment would believe it.
- **Completion is judged, not asserted.** `submit_research_record` assembles
  the record from the ledger, runs the completion contract, and reports the
  requirements that are unmet. INCOMPLETE with a list of what is missing is the
  answer the contract is designed to produce.

Nothing here creates, cancels or re-points a node, writes a Decision Record, or
writes a contract. The two status moves that do happen — into RUNNING at the
start, into REVIEWING at the hand-over — go through the same life-cycle path
every other node takes, so neither is a second way to move one. Research
produces evidence and advice; what the project does about either is Master's.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from ravel.config import get_settings
from ravel.domain.clock import json_iso
from ravel.domain.contracts import ExecutionContract
from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    AccessStatus,
    ClaimClass,
    Confidence,
    EvidenceSourceTier,
    NodeStatus,
    NodeType,
    ReviewOutcome,
)
from ravel.domain.evidence import (
    Evidence,
    EvidenceConflict,
    EvidenceSource,
    claim_access,
    claim_tier,
)
from ravel.domain.state_machines import ACTIVE_NODE_STATUSES
from ravel.mcp.context import ToolContext, as_json, require_research
from ravel.research import completion, sufficiency
from ravel.research.gateway import ResearchSourceGateway, SourceRequest
from ravel.research.leads import Lead, Retrieval
from ravel.research.opened import OpenedSources
from ravel.state.repositories.contracts import (
    ExecutionContractRepository,
    ResearchContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.research import (
    EvidenceConflictRepository,
    EvidenceRepository,
    EvidenceSourceRepository,
    ResearchRecordRepository,
    task_ledger,
)
from ravel.state.store import ArtifactStore, S3ArtifactStore

#: What this process has opened and not yet registered. One per tool server,
#: because a retrieval is bytes this process read and a reference is only
#: meaningful to whoever is holding them.
OPENED = OpenedSources()

#: How much of a source's own text an `open_source` result carries. Enough for
#: a reader to see it is what it claims to be and for a claim to be written
#: from it; the whole body is in the snapshot when a snapshot was taken, and
#: sending megabytes through a model's context is not research.
EXCERPT_CHARS = 4000


def _research_node(session: Any, project_id: str, node_id: str) -> DagNode:
    """The RESEARCH node a task is about.

    Refused rather than accepted when the node is another type: a Research
    session asked to work on a COMPUTATION node is being asked to do something
    the project's own DAG says belongs to a Worker, and doing it would put the
    ledger's contents behind a role that does not hold that work's contract.

    Raises:
        ValueError: The node is not a RESEARCH node.
    """
    node = DagRepository(session, project_id).node(node_id)
    if node.node_type is not NodeType.RESEARCH:
        raise ValueError(
            f"{node.display_id} is a {node.node_type.value} node, and this session "
            "researches; the Evidence Ledger is written by the role whose task is "
            "the question being answered, and this node's work belongs to "
            f"{node.executor_role.value}"
        )
    return node


def _gateway(context: ToolContext, session: Any) -> ResearchSourceGateway:
    """The gateway this session reads the world through.

    Built per call rather than held, so that a session which never searches
    never constructs a connector — and so that a settings change between calls
    (a search provider key appearing, say) is picked up rather than cached
    against. The store is the exception: it is built once per process, because
    a source registered in one call is snapshotted by the next, and building a
    second object-store client per call buys nothing.
    """
    return ResearchSourceGateway(
        session=session,
        project_id=context.project_id,
        settings=get_settings(),
        store=_store(),
    )


@lru_cache(maxsize=1)
def _store() -> ArtifactStore | None:
    """The object store snapshots go to, or `None` if it cannot be built.

    Degrading rather than failing. A source can be registered without a
    snapshot — the hash is recomputed from the bytes RAVEL read either way —
    and what a missing snapshot costs is reproducibility, which the sufficiency
    assessment measures and reports as an axis of its own. A research task that
    could record no sources at all because an object store was misconfigured
    would be a task that reports the gap as "nothing found".
    """
    try:
        return S3ArtifactStore()
    except Exception:  # a store that cannot be built is the degradation above
        return None


def read_research_task(context: ToolContext) -> Any:
    """What this task is answering, under what terms, and what is recorded."""

    async def read_research_task(node_id: str) -> dict[str, Any]:
        """Read one research task: its question, its terms, and its ledger.

        `terms` is the frozen Execution Contract this task runs under — the
        actions it may take and the outputs it owes. `project` carries the
        Research Contract, which is what the user actually asked for and the
        reason the question was asked at all.

        `ledger` is what has been recorded so far: sources registered, claims
        by class, conflicts, and any research record already submitted. Read it
        before searching, so that a second pass adds to the evidence rather
        than re-deriving it.
        """
        require_research(context, "read_research_task")
        with context.read() as session:
            node = _research_node(session, context.project_id, node_id)
            contract = ResearchContractRepository(session, context.project_id).current()
            terms = ExecutionContractRepository(session, context.project_id).for_node(
                node_id
            )
            ledger = task_ledger(session, context.project_id, node_id)

        return {
            "node": as_json(node),
            "project": {
                "original_user_goal": contract.original_user_goal,
                "scientific_problem": contract.scientific_problem,
                "research_hypotheses": list(contract.research_hypotheses),
                "target_metrics": list(contract.target_metrics),
                "acceptance_strategy": contract.acceptance_strategy,
                "known_constraints": list(contract.known_constraints),
                "prohibited_actions": list(contract.prohibited_actions),
            },
            "terms": _terms(terms),
            "ledger": {
                "sources": as_json(ledger.sources),
                "claims": as_json(ledger.claims),
                "conflicts": as_json(ledger.conflicts),
                "records": as_json(ledger.records),
            },
        }

    return read_research_task


def _terms(contract: ExecutionContract) -> dict[str, Any]:
    """The frozen terms, without the parts a researcher does not act on."""
    return {
        "contract_id": contract.contract_id,
        "version": contract.version,
        "objective": contract.objective,
        "procedure": contract.procedure,
        "allowed_actions": list(contract.allowed_actions),
        "required_outputs": list(contract.required_outputs),
        "stop_conditions": list(contract.stop_conditions),
        "prohibited": [
            "registering a claim RAVEL did not read a source for",
            "treating a search result, snippet or model recollection as provenance",
        ],
        "is_frozen": contract.is_frozen,
    }


def begin_research(context: ToolContext) -> Any:
    """Begin the task this Research session was convened for."""

    async def begin_research(node_id: str) -> dict[str, Any]:
        """Begin this research task, and take it into your hands.

        **This is where a research task begins.** A RESEARCH node runs because
        the Research Agent whose node it is asked for it — the same chain the
        Workers follow through `start_execution`, and the reason no other role
        holds this tool: work begins as the act of the seat that executes it,
        never as something done to the seat from outside.

        **What it does not do is start anything for you.** A Worker's start
        hands the work to RAVEL's Execution Service, which runs it durably on a
        machine while the Worker goes back to watching. Research's work is the
        reading itself, and the reading happens in this session: the searching,
        the opening and the ledger writes are the tools in your roster. There is
        no run to hand over and no workflow behind you, which is why this tool
        moves the node and returns.

        **A refusal is not a fault.** The DAG is the authority on whether this
        node may run — it must be READY, with its Execution Contract bound —
        and the answer says which condition was not met. Do not look for another
        way to begin it; report the refusal and stop.

        After this returns, do the task: read it with `read_research_task`,
        gather the evidence, and finish with `submit_research_record`, which
        hands the result over.
        """
        require_research(context, "begin_research")
        with context.write() as session:
            dag = DagRepository(session, context.project_id)
            node = _research_node(session, context.project_id, node_id)
            # The same two questions `start_execution` asks, read from the same
            # places: may this node be RUNNING (`can_enter_running`, which is
            # where the bound contract is checked), and is it waiting to begin
            # at all — a run begins from READY, and the state machine's
            # tolerance for a re-asserted status is not a second way in.
            refusal = _may_this_task_begin(
                node,
                has_frozen_acceptance=dag.has_frozen_acceptance(node_id),
                has_execution_contract=dag.has_execution_contract(node_id),
                pre_run_outcome=dag.latest_pre_run_outcome(node_id),
            )
            if refusal is not None:
                return {
                    "node_id": node.node_id,
                    "started": False,
                    "refused": True,
                    "reason": refusal,
                    "node_status": node.status.value,
                }
            moved = dag.transition_node(
                node_id, NodeStatus.RUNNING, actor_id=context.role.value
            )
        return {
            "node_id": moved.node_id,
            "node_status": moved.status.value,
            "started": True,
            "refused": False,
            "what_happens_next": (
                "The task is yours and nobody else will move it. Read it with "
                "read_research_task, gather the evidence, and hand the result over "
                "with submit_research_record."
            ),
        }

    return begin_research


def _may_this_task_begin(
    node: DagNode,
    *,
    has_frozen_acceptance: bool,
    has_execution_contract: bool,
    pre_run_outcome: ReviewOutcome | None,
) -> str | None:
    """Why this node may not begin, or `None` if it may.

    Two questions, asked in this order because the first is about the node's
    place in its own life and the second about whether the DAG has cleared it.
    **Is it waiting to begin** — a task enters RUNNING from READY, and a node
    already in flight must not be "begun" a second time, which is why the
    answer says what to read instead. **Does the DAG say it may run** — asked
    through `DagNode.can_enter_running`, the same predicate every other entry
    to RUNNING is checked against, so this tool cannot become a way round the
    contract gate by being a different caller.

    A reason rather than a bool: the model reads it, and "the contract is not
    bound" is something it can report, while "no" is not.
    """
    if node.status is not NodeStatus.READY:
        return (
            f"this task is {node.status.value}, and a task begins from READY. "
            + (
                "It is already in flight, so it is being worked on: read its "
                "record and carry on with it rather than beginning it again."
                if node.status in ACTIVE_NODE_STATUSES
                else "It is not waiting to begin, so what happens to it next is "
                "not yours to start."
            )
        )
    check = node.can_enter_running(
        has_frozen_acceptance=has_frozen_acceptance,
        has_execution_contract=has_execution_contract,
        pre_run_outcome=pre_run_outcome,
    )
    return None if check.allowed else check.reason


def search_sources(context: ToolContext) -> Any:
    """Ask the structured connectors what they have."""

    async def search_sources(
        query: str,
        sources: list[str] | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Search the bibliographic and chemical databases RAVEL is configured for.

        Returns *leads*: pointers with titles and identifiers, and no guarantee
        that anything is behind them. A lead is never evidence. Open the ones
        worth reading with `open_source`, and register those with
        `register_source`.

        `sources` names which connectors to consult — the returned
        `available_connectors` lists them — and defaults to all of them.
        `unavailable` reports connectors that could not answer, which is part
        of the result: a search that returned nothing because a service was
        down is not a search that found nothing.
        """
        require_research(context, "search_sources")
        with context.read() as session:
            gateway = _gateway(context, session)
            outcome = gateway.search(
                query, sources=tuple(sources) if sources else None, limit=limit
            )
            available = sorted(gateway.connectors)
        return {
            "query": outcome.query,
            "leads": [_lead(lead) for lead in outcome.leads],
            "unavailable": list(outcome.unavailable),
            "available_connectors": available,
        }

    return search_sources


def search_web(context: ToolContext) -> Any:
    """Search the open web through the configured provider."""

    async def search_web(query: str, limit: int = 10) -> dict[str, Any]:
        """Search the web, and say so plainly when that is not possible.

        Same contract as `search_sources`: what comes back are leads. Web
        results are the weakest thing RAVEL will hand a researcher, and the
        only way they become evidence is by being opened and read.

        Raises:
            ValueError: No search provider is configured. A refuse rather than
                an empty result, because "RAVEL cannot search the web" and "the
                web has nothing on this" are different findings, and the second
                one would be a fabrication.
        """
        require_research(context, "search_web")
        with context.read() as session:
            gateway = _gateway(context, session)
            try:
                outcome = gateway.search_web(query, limit=limit)
            except Exception as exc:  # SearchUnavailable, or the provider's own fault
                raise ValueError(
                    f"RAVEL's web search is not usable: {exc}. Report this as a gap "
                    "in what you could establish rather than as a search that found "
                    "nothing — configure a provider, or work from the structured "
                    "connectors."
                ) from exc
        return {
            "query": outcome.query,
            "leads": [_lead(lead) for lead in outcome.leads],
            "unavailable": list(outcome.unavailable),
        }

    return search_web


def _lead(lead: Lead) -> dict[str, Any]:
    """One lead as JSON, with what it is said plainly."""
    return {**as_json(lead), "is_evidence": False}


def open_source(context: ToolContext) -> Any:
    """Open a URL and report what actually came back."""

    async def open_source(
        url: str,
        title: str = "",
        publisher: str = "",
        doi: str | None = None,
        declared_type: str | None = None,
        provider: str = "",
        rendered: bool = False,
    ) -> dict[str, Any]:
        """Fetch a URL through the gateway and hold what came back.

        Nothing is written to the ledger yet: this is the reading, and
        `register_source` is the decision that what was read is a source. The
        result carries the `retrieval_ref` that call needs.

        `access_status` is the honest answer about a source RAVEL could not
        read — PAYWALLED, AUTH_REQUIRED, ACCESS_LIMITED, POLICY_BLOCKED — and
        such a source can still be registered. "This exists and RAVEL could not
        read it" is a finding; inventing its contents from an abstract, or from
        what you already believe, is the one thing that must not happen.

        `declared_type` is the classification the *record* gave — Crossref's
        `journal-article` and so on — and it outranks the host when the tier is
        assigned. Pass it when a connector supplied one, and leave it out
        otherwise: a type you inferred from the URL is not a declaration.

        `rendered=True` runs the page in a headless browser and reads the
        rendered document, for a page that fills itself in with script. It is
        slower and is tried after plain fetching has already failed to yield
        text.
        """
        require_research(context, "open_source")
        request = SourceRequest(
            url=url,
            title=title,
            publisher=publisher,
            doi=doi,
            declared_type=declared_type,
            provider=provider,
        )
        with context.read() as session:
            retrieval = _gateway(context, session).open(request, rendered=rendered)
        reference = OPENED.remember(retrieval)
        return _retrieval(reference, retrieval)

    return open_source


def _retrieval(reference: str, retrieval: Retrieval) -> dict[str, Any]:
    """One retrieval as the researcher sees it."""
    return {
        "retrieval_ref": reference,
        "requested_url": retrieval.requested_url,
        "final_url": retrieval.final_url,
        "access_status": retrieval.access_status.value,
        "status_code": retrieval.status_code,
        "media_type": retrieval.media_type,
        "title": retrieval.title,
        "content_hash": retrieval.content_hash,
        "size_bytes": retrieval.size_bytes,
        "retrieved_at": json_iso(retrieval.retrieved_at),
        "excerpt": retrieval.excerpt[:EXCERPT_CHARS],
        "excerpt_truncated": len(retrieval.excerpt) > EXCERPT_CHARS,
        "note": retrieval.note,
    }


def register_source(context: ToolContext) -> Any:
    """Write a source RAVEL read into the Evidence Ledger."""

    async def register_source(
        retrieval_ref: str,
        node_id: str,
        title: str = "",
        publisher: str = "",
        doi: str | None = None,
        declared_type: str | None = None,
        provider: str = "",
        snapshot: bool = True,
    ) -> dict[str, Any]:
        """Register one opened source: the ledger's only entrance.

        `retrieval_ref` is the reference `open_source` returned. It names bytes
        this process is holding, and there is no way to register anything else:
        RAVEL hashes what it read and stores what it hashed, and a reference it
        cannot resolve is refused rather than reconstructed.

        Re-registering an already-registered URL is allowed and writes a second
        row: reading a source twice at two moments is two facts, and a source
        that changed between them is exactly what the second reading exists to
        show.

        `snapshot=True` stores the bytes themselves, so that a later reader can
        check the claim against what was read rather than against whatever the
        URL serves then. It is skipped when no artifact store is configured,
        and the result says whether it happened.
        """
        require_research(context, "register_source")
        retrieval = OPENED.recall(retrieval_ref)
        if retrieval is None:
            raise ValueError(
                f"this session is not holding a retrieval called {retrieval_ref!r}; a "
                "source is registered from bytes RAVEL read and still has, and this "
                "reference is either misspelled or from a source that has been "
                "evicted. Open the URL again with open_source and register that."
            )
        request = SourceRequest(
            url=retrieval.requested_url,
            title=title or retrieval.title,
            publisher=publisher,
            doi=doi,
            declared_type=declared_type,
            provider=provider,
            node_id=node_id,
        )
        with context.write() as session:
            _research_node(session, context.project_id, node_id)
            registration = _gateway(context, session).register(
                request,
                retrieval,
                actor_id=context.role.value,
                node_id=node_id,
                research_task_ref=node_id,
                snapshot=snapshot,
            )
        # The two acts are done: opened, then registered. Holding the bytes past
        # this point buys nothing — a second registration of one reading would
        # be two rows for one read — and a session that opens hundreds of
        # sources should not pay for the ones it has already written down.
        OPENED.forget(retrieval_ref)
        return {
            "source": as_json(registration.source),
            "tier": registration.tier.as_dict(),
            "was_read": registration.was_read,
            "snapshot_ref": registration.snapshot_ref,
        }

    return register_source


def record_evidence(context: ToolContext) -> Any:
    """Write one claim, with the class that says what kind of claim it is."""

    async def record_evidence(
        node_id: str,
        statement: str,
        claim_class: str,
        source_refs: list[str] | None = None,
        conditions: dict[str, str] | None = None,
        confidence: str = "MEDIUM",
        conflicts_with: list[str] | None = None,
    ) -> dict[str, Any]:
        """Record one claim about the world, and what supports it.

        `claim_class` decides what may be said. A **FACT** must cite sources
        RAVEL read — the domain refuses one that cites nothing, or that rests
        only on something it could not open. An **INFERENCE** must say what it
        was inferred from and under `conditions`, which is where the reasoning
        stops applying. A **HYPOTHESIS** may rest on nothing yet, which is what
        makes it one.

        The claim's tier and access status are **derived from its sources**,
        not given: the strongest tier among the sources RAVEL read, and whether
        any of them was readable at all. You cannot file a blog post as tier A,
        and there is nothing to gain by trying.

        `conflicts_with` names other claims this one disagrees with. Record the
        disagreement, and then `record_conflict` it — a disagreement nobody
        wrote down is not a finding, and the sufficiency assessment reads the
        conflict records, not your intent.
        """
        require_research(context, "record_evidence")
        parsed = _claim_class(claim_class)
        with context.write() as session:
            _research_node(session, context.project_id, node_id)
            sources = EvidenceSourceRepository(session, context.project_id)
            named = [sources.get(source_id=ref) for ref in source_refs or ()]
            tier, access = _rating(named)
            evidence = Evidence(
                project_id=context.project_id,
                statement=statement,
                claim_class=parsed,
                source_tier=tier,
                access_status=access,
                source_refs=tuple(source_refs or ()),
                conditions=dict(conditions or {}),
                confidence=_confidence(confidence),
                conflicts_with=tuple(conflicts_with or ()),
                research_task_ref=node_id,
                retrieved_at=max(
                    (source.retrieved_at for source in named if source.retrieved_at),
                    default=None,
                ),
                content_hash=next(
                    (source.content_hash for source in named if source.content_hash),
                    None,
                ),
            )
            written = EvidenceRepository(session, context.project_id).register(
                evidence, actor_id=context.role.value
            )
        return as_json(written)

    return record_evidence


def record_conflict(context: ToolContext) -> Any:
    """Write down two claims that disagree, without resolving them."""

    async def record_conflict(
        evidence_refs: list[str],
        description: str,
        condition_difference: str = "",
    ) -> dict[str, Any]:
        """Record a disagreement between claims, preserved rather than averaged.

        `evidence_refs` names the claims that disagree — at least two — and
        `description` says what they disagree *about*. `condition_difference`
        is where the difference actually lies, if it does: two measurements
        taken under different conditions may not conflict at all, and saying so
        is more useful than leaving a reader to guess which of them is wrong.

        A conflict is a finding, not a fault. Averaging conflicting evidence,
        or keeping only the convenient half, would hide exactly the uncertainty
        a decision has to account for — and the sufficiency assessment counts
        unresolved conflicts against the evidence on purpose.
        """
        require_research(context, "record_conflict")
        refs = tuple(dict.fromkeys(evidence_refs))
        if len(refs) < 2:
            raise ValueError(
                f"a conflict is between at least two claims and this names {len(refs)}; "
                "one claim disagreeing with nothing is not a conflict"
            )
        with context.write() as session:
            ledger = EvidenceRepository(session, context.project_id)
            # Read rather than trusted: a conflict between claims that do not
            # exist would be a disagreement about nothing.
            named = [ledger.get(evidence_id=ref) for ref in refs]
            description_parts = [description.strip()]
            if condition_difference.strip():
                description_parts.append(f"condition difference: {condition_difference.strip()}")
            conflict = EvidenceConflict(
                project_id=context.project_id,
                evidence_refs=refs,
                description="; ".join(part for part in description_parts if part),
            )
            written = EvidenceConflictRepository(session, context.project_id).add(conflict)
        return {
            **as_json(written),
            "disagrees_about": [claim.statement for claim in named],
        }

    return record_conflict


def assess_evidence(context: ToolContext) -> Any:
    """Measure what has been gathered against the six considerations."""

    async def assess_evidence(node_id: str, rationale: str = "") -> dict[str, Any]:
        """Assess whether the evidence can carry the decision it is for.

        The six axes of `docs/05` §7 — independence, authority, directness,
        condition match, reproducibility, conflict — measured against the
        ledger, with the category they support. Nothing is written: this is the
        question "is there enough yet", asked before the record is submitted,
        and it is meant to be asked more than once.

        `would_change_with` is the actionable half: what would move each axis
        that is holding the category down. Read it as the next search, not as a
        list of complaints. `rationale` is your own note on the assessment,
        which is stored with it when the record is submitted.
        """
        require_research(context, "assess_evidence")
        with context.read() as session:
            _research_node(session, context.project_id, node_id)
            ledger = task_ledger(session, context.project_id, node_id)
        assessment = sufficiency.assess(
            claims=list(ledger.claims),
            sources=list(ledger.sources),
            conflicts=list(ledger.conflicts),
        )
        return {
            **_assessment(assessment),
            "rationale_note": rationale,
            "sources_read": len([source for source in ledger.sources if source.was_read]),
            "sources_named": len(ledger.sources),
        }

    return assess_evidence


def _assessment(assessment: sufficiency.Assessment) -> dict[str, Any]:
    """An assessment as the researcher sees it, measurements included."""
    return {
        "sufficiency": assessment.sufficiency.value,
        "measurements": [
            {
                "consideration": measurement.consideration.value,
                "finding": measurement.finding.value,
                "detail": measurement.detail,
            }
            for measurement in assessment.measurements
        ],
        "gaps": list(assessment.gaps),
        "would_change_with": list(assessment.would_change_with),
    }


def submit_research_record(context: ToolContext) -> Any:
    """Judge the task against the completion contract, and write the record."""

    async def submit_research_record(
        node_id: str,
        question: str = "",
        suggested_acceptance_criteria: list[str] | None = None,
        recommended_followups: list[str] | None = None,
        unknowns: list[str] | None = None,
        report: str = "",
    ) -> dict[str, Any]:
        """Submit the structured record for this research task.

        The record is assembled from the ledger — its facts, inferences and
        hypotheses are the claims you recorded, and its sources are the ones
        they rest on — and then judged against the nine requirements of
        `docs/05` §11. The `completion_status` that comes back is RAVEL's
        answer, not yours: COMPLETE only when every requirement holds, and
        INCOMPLETE with the `unmet` list otherwise.

        **INCOMPLETE is a correct answer.** A task that comes back with a clear
        list of what it could not establish has done its job; a task that comes
        back COMPLETE with a story has not. When a requirement is unmet, the
        detail says what was actually found, so you can decide whether to search
        more or to return what you have with the gap named.

        `unknowns` is what you could not establish. `recommended_followups` is
        required even when the task is complete — a finished question usually
        opens the next one. `suggested_acceptance_criteria` must have something
        observed behind it: a threshold nobody measured is an expectation, and
        it is refused rather than recorded as a benchmark.

        `report` is the human-readable projection. The structured record is the
        authoritative one; Master reads that first.

        **Submitting hands the task over.** In the same transaction that writes
        the record, the node moves from RUNNING to REVIEWING, where the FINAL
        checkpoint judges it against the frozen terms. INCOMPLETE is handed over
        like any other answer: whether the evidence is enough is Review's
        question, and a task that could keep itself running until its author
        felt finished would be holding its own finish line.
        """
        require_research(context, "submit_research_record")
        with context.write() as session:
            dag = DagRepository(session, context.project_id)
            node = _research_node(session, context.project_id, node_id)
            ledger = task_ledger(session, context.project_id, node_id)
            claims = list(ledger.claims)
            sources = list(ledger.sources)
            conflicts = list(ledger.conflicts)
            assessment = sufficiency.assess(
                claims=claims, sources=sources, conflicts=conflicts
            )
            draft = completion.Draft(
                question=question or node.objective,
                claims=claims,
                sources=sources,
                conflicts=conflicts,
                suggested_acceptance_criteria=tuple(suggested_acceptance_criteria or ()),
                recommended_followups=tuple(recommended_followups or ()),
                unknowns=tuple(unknowns or ()),
                report=report,
                task_id=node_id,
                node_id=node_id,
            )
            judged = completion.judge(draft, assessment)
            record = completion.build(draft, assessment, project_id=context.project_id)
            written = ResearchRecordRepository(session, context.project_id).add(record)
            # The hand-over, written in the same transaction as the record: a
            # crash between the two would be a record nobody was told about.
            # From RUNNING only, because that is where a task that is being
            # worked on is — a node that is already REVIEWING has been handed
            # over, and a node that never began is reported rather than moved
            # into a checkpoint that would be judging work nobody started.
            handed_over = node.status is NodeStatus.RUNNING
            moved = node
            if handed_over:
                moved = dag.transition_node(
                    node_id, NodeStatus.REVIEWING, actor_id=context.role.value
                )
        return {
            "record": as_json(written),
            "completion_status": judged.status.value,
            "unmet": list(judged.unmet),
            "detail": list(judged.detail),
            "assessment": _assessment(assessment),
            "unmeasured": [item.value for item in completion.unmeasured(assessment)],
            "is_complete": judged.is_complete,
            "handed_over": handed_over,
            "node_status": moved.status.value,
            "hand_over_reason": (
                None
                if handed_over
                else (
                    f"{node.display_id} is {node.status.value} and a research task is "
                    "handed over from RUNNING. The record is written either way; call "
                    "begin_research if the task has not been begun."
                )
            ),
        }

    return submit_research_record


def _rating(named: list[EvidenceSource]) -> tuple[EvidenceSourceTier, AccessStatus]:
    """The tier and access status a claim resting on these sources is due.

    A claim that names no source is the case `claim_tier` and `claim_access`
    decline to rate: they read a claim's standing off its sources, and there
    are none to read. It is recorded at the weakest tier rather than refused,
    because a hypothesis resting on nothing yet is what the HYPOTHESIS class
    exists for — and the weakest tier is the one `tiers.assign` gives anything
    it cannot classify, so an unsupported claim is rated by the same default as
    an unclassified source. Access is `OK` because nothing was unreachable:
    there was nothing to reach.
    """
    if not named:
        return EvidenceSourceTier.D, AccessStatus.OK
    return claim_tier(named), claim_access(named)


def _claim_class(value: str) -> ClaimClass:
    """The claim class the model named.

    Raises:
        ValueError: It is not one of the three. Named here so the message lists
            the ones that exist.
    """
    try:
        return ClaimClass(value)
    except ValueError as exc:
        known = ", ".join(level.value for level in ClaimClass)
        raise ValueError(f"claim_class must be one of {known}; got {value!r}") from exc


def _confidence(value: str) -> Confidence:
    """The confidence band the model named.

    Raises:
        ValueError: It is not one of the three bands.
    """
    try:
        return Confidence(value)
    except ValueError as exc:
        known = ", ".join(level.value for level in Confidence)
        raise ValueError(f"confidence must be one of {known}; got {value!r}") from exc


IMPLEMENTATIONS: dict[str, Any] = {
    "begin_research": begin_research,
    "read_research_task": read_research_task,
    "search_sources": search_sources,
    "search_web": search_web,
    "open_source": open_source,
    "register_source": register_source,
    "record_evidence": record_evidence,
    "record_conflict": record_conflict,
    "assess_evidence": assess_evidence,
    "submit_research_record": submit_research_record,
}
