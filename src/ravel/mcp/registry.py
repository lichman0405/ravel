"""Which tools each role may reach.

This table is the authorization record. A tool that is not listed for a role is
never registered in that role's server process, so the agent cannot call it —
the model's tool list and RAVEL's grant are the same object, not two things
kept in sync.

Each phase widens its own role's roster, and none may widen another's beyond
what that role's behavioral contract already permits. The one rule that holds
across every phase is stated once, below, as `DAG_MUTATION_TOOLS`: no role but
Master appears in any of those rows, and the integration suite asserts it
against the running servers rather than against this file.
"""

from __future__ import annotations

from ravel.domain.roles import AgentRole

MASTER = AgentRole.MASTER
RESEARCH = AgentRole.RESEARCH
REVIEW = AgentRole.REVIEW
COMPUTE_WORKER = AgentRole.COMPUTE_WORKER
EXPERIMENTAL_WORKER = AgentRole.EXPERIMENTAL_WORKER

#: Every role may confirm its own scope. An agent that cannot tell which project
#: and role it is serving cannot report a scoping fault.
_EVERY_ROLE = frozenset({MASTER, RESEARCH, REVIEW, COMPUTE_WORKER, EXPERIMENTAL_WORKER})

#: Tool name -> the roles whose server registers it.
TOOL_ROLES: dict[str, frozenset[AgentRole]] = {
    "whoami": _EVERY_ROLE,
    # ── Master: reading the project, and changing the plan ──────────────────
    #
    # Reading authoritative state is Master's alone. Workers receive a frozen
    # contract; Review receives the artifact under review; neither needs the
    # whole project, and giving it to them would let a worker re-scope its own
    # task.
    "read_project_state": frozenset({MASTER}),
    "list_pending_approvals": frozenset({MASTER}),
    "read_master_checkpoint": frozenset({MASTER}),
    "write_master_checkpoint": frozenset({MASTER}),
    "add_dag_node": frozenset({MASTER}),
    "expand_dag_phase": frozenset({MASTER}),
    "cancel_dag_node": frozenset({MASTER}),
    # ── Review: judging work against the criteria it was frozen against ─────
    #
    # Review reads the record and writes a verdict, and that is the whole of
    # its surface. It is not given the project state: the question it answers
    # is about one node, and a reviewer holding the whole plan would be a
    # reviewer in a position to reason about what the plan *should* be — which
    # is Master's question, not Review's.
    "read_review_work": frozenset({REVIEW}),
    "read_review_package": frozenset({REVIEW}),
    "submit_review": frozenset({REVIEW}),
    # ── Research: the one way into the Evidence Ledger ─────────────────────
    #
    # Reading the task and searching are separate from writing, and the writing
    # tools are all Research's. A source row is a statement that RAVEL read
    # something, and the role that makes it is the role whose task asked the
    # question — which is why no other role holds any of these.
    "read_research_task": frozenset({RESEARCH}),
    "search_sources": frozenset({RESEARCH}),
    "search_web": frozenset({RESEARCH}),
    "open_source": frozenset({RESEARCH}),
    "register_source": frozenset({RESEARCH}),
    "record_evidence": frozenset({RESEARCH}),
    "record_conflict": frozenset({RESEARCH}),
    "assess_evidence": frozenset({RESEARCH}),
    "submit_research_record": frozenset({RESEARCH}),
    # ── Workers: what they act under, and what they may say ────────────────
    #
    # Both Workers hold the same three, because both act under a frozen
    # Execution Contract and both may find that it does not name what they have
    # been asked for. `send_message` is the Experimental Worker's alone: it is
    # the role that talks to a lab, and the four permitted kinds are the whole
    # of what a Worker may say to one.
    "read_execution_contract": frozenset({COMPUTE_WORKER, EXPERIMENTAL_WORKER}),
    "read_execution_status": frozenset({COMPUTE_WORKER, EXPERIMENTAL_WORKER}),
    "request_action": frozenset({COMPUTE_WORKER, EXPERIMENTAL_WORKER}),
    "send_message": frozenset({EXPERIMENTAL_WORKER}),
}

#: The tools that change the Scientific DAG. Master is the only role that holds
#: DAG mutation authority, and this set is what makes that checkable: the
#: integration suite asserts that no other role's server registers one of
#: these, and that the ones Master's server registers refuse a non-Master scope
#: even if they were ever reachable.
DAG_MUTATION_TOOLS: frozenset[str] = frozenset(
    {"add_dag_node", "expand_dag_phase", "cancel_dag_node"}
)

#: Which role may hold which writing tool — the record each is the author of.
#:
#: Master writes the plan and the decisions that order it; Review writes
#: verdicts; Research writes the Evidence Ledger; the Workers write what they
#: said and what they were refused. These are four different records of four
#: different acts, and no role may write another's.
#:
#: Two of these are writes and deliberately not DAG mutations. `submit_review`
#: records what Review found: the one status change that follows a final
#: verdict is RAVEL applying it, not Review changing the plan. `request_action`
#: and `send_message` record what a Worker asked and what it was told — the
#: deviation is a question, and answering it is Master's.
#:
#: A role absent from this table holds no writing tool at all, and the two
#: Workers are the case that shows why: a Worker that needs to write a record
#: is a role whose authorship has to be added here deliberately, rather than a
#: role that quietly acquired a tool. The same holds in the other direction —
#: `request_action` is held by both Workers, and this table is where that is
#: stated rather than where it is discovered.
WRITE_AUTHORSHIP: dict[AgentRole, frozenset[str]] = {
    MASTER: DAG_MUTATION_TOOLS | {"write_master_checkpoint"},
    REVIEW: frozenset({"submit_review"}),
    RESEARCH: frozenset(
        {
            "register_source",
            "record_evidence",
            "record_conflict",
            "submit_research_record",
        }
    ),
    COMPUTE_WORKER: frozenset({"request_action"}),
    EXPERIMENTAL_WORKER: frozenset({"request_action", "send_message"}),
}

#: The tools that write anything at all. Keeping the set explicit means a read
#: tool that starts writing has to be noticed here, and a writing tool handed
#: to a role that is not its author has to be noticed in the table above.
WRITE_TOOLS: frozenset[str] = frozenset().union(*WRITE_AUTHORSHIP.values())

#: Model-facing descriptions. Written from the agent's point of view: what the
#: tool returns and when to reach for it, with no transport or RAVEL vocabulary.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "whoami": (
        "Report the project and role this session is serving, and the tools available to it. "
        "Use this to confirm your scope before acting."
    ),
    "read_project_state": (
        "Read the current authoritative state of this project: its objective and status, its "
        "roadmap and where in it the project is now, a summary of the DAG by status, and how "
        "many approvals are waiting on a human. This is the record of truth — prefer it over "
        "your own recollection of earlier conversation."
    ),
    "list_pending_approvals": (
        "List the approvals no human has answered yet, with what each is waiting on. Use it "
        "to avoid asking twice, and to tell waiting-on-a-human apart from waiting-on-yourself."
    ),
    "read_master_checkpoint": (
        "Read the most recent checkpoint written for this project. On recovery, read this "
        "first and then re-derive every claim in it from the project state before acting."
    ),
    "write_master_checkpoint": (
        "Write down where your work stands — current focus, open hypotheses, what you are "
        "waiting on, and which decisions a successor should read — so a replacement session "
        "can continue after this one is gone."
    ),
    "add_dag_node": (
        "Add one node to the Scientific DAG with the reason it is being added, and with "
        "the terms it will run under. The node is recorded against a decision that "
        "names it, so supply a real rationale. Its dependencies are fixed when it is "
        "created. A COMPUTATION or EXPERIMENT node needs `criteria` — what would make "
        "its result acceptable, each with the provenance that says where it came "
        "from — and every node needs the actions it may take and the outputs it owes."
    ),
    "expand_dag_phase": (
        "Commit the concrete work a roadmap stage consists of, as one decision, each "
        "node with the criteria it will be measured against and the terms it runs "
        "under. Use this to turn the current stage into executable nodes; nodes in one "
        "call may depend on each other."
    ),
    "cancel_dag_node": (
        "Cancel a node that should not happen, recording the decision that ended it. This is "
        "for work that has not produced a result; a node that already passed or failed has a "
        "result, and cancelling is not how a result is undone."
    ),
    "read_review_work": (
        "List the nodes a verdict is waiting on now, and the ones a verdict could be given "
        "about but is not required. Each entry names the checkpoint that applies and the "
        "frozen contract the verdict must be about. Start here, then read the package for "
        "the node you are about to judge."
    ),
    "read_review_package": (
        "Read everything one node's verdict has to be measured against: its objective and "
        "status, the criteria frozen before it ran (or, for a node type that has none, the "
        "Execution Contract it ran under), the record of what actually executed, the "
        "artifacts it produced, and the verdicts already given about it. Quote the criteria "
        "by their identifiers in your verdict."
    ),
    "submit_review": (
        "Record a verdict on one node at one checkpoint. A FINAL verdict ends the node — it "
        "becomes PASSED, FAILED or PARTIAL and stays that way. A RUNTIME verdict is advice "
        "and moves nothing. Report a final verdict on every frozen criterion one by one, "
        "with what you observed; a verdict that leaves one out is refused."
    ),
    "read_research_task": (
        "Read the research task you are serving: what it is answering, the terms it runs "
        "under, and everything already in the ledger for it. Read this first, and again "
        "before submitting, so that a second pass adds evidence rather than re-deriving it."
    ),
    "search_sources": (
        "Search the bibliographic and chemical databases for what is known about a "
        "question. Returns leads, with their identifiers and titles — a lead is a pointer, "
        "not evidence, and nothing is in the ledger until you open it and register it. "
        "Reports which services could not answer, which is part of the result."
    ),
    "search_web": (
        "Search the open web. Returns leads, of the weakest kind: web results become "
        "evidence only by being opened and read. Refuses rather than returning nothing "
        "when no search provider is configured, because 'RAVEL cannot search' and 'the web "
        "has nothing' are different findings."
    ),
    "open_source": (
        "Fetch a URL and read what actually comes back: the status, the media type, the "
        "hash of the bytes, and the beginning of the text. Nothing is recorded in the "
        "ledger yet — registering is a separate, deliberate act. A source RAVEL could not "
        "read comes back saying so (PAYWALLED, AUTH_REQUIRED and so on), and that is a "
        "finding worth registering; inventing what it says is not."
    ),
    "register_source": (
        "Write a source you opened into the Evidence Ledger, with the tier RAVEL assigned "
        "it and a stored copy of the bytes. This is the only way anything enters the "
        "ledger: the source is registered from the bytes RAVEL read, never from a URL and "
        "a hash you supply."
    ),
    "record_evidence": (
        "Record one claim and what supports it. A FACT needs sources you actually read; "
        "an INFERENCE says what it was inferred from and the conditions it holds under; a "
        "HYPOTHESIS may rest on nothing yet. The claim's tier and access status are read "
        "from its sources, so there is nothing to gain by asserting them."
    ),
    "record_conflict": (
        "Record that two claims disagree, and what the disagreement is about. Conflicts "
        "are preserved rather than averaged away: a disagreement nobody wrote down cannot "
        "be reviewed, and unresolved ones count against the sufficiency of the evidence."
    ),
    "assess_evidence": (
        "Measure what you have gathered against the six considerations — independence, "
        "authority, directness, condition match, reproducibility, conflict — and say "
        "whether it can carry the decision it is for. Writes nothing. Ask it more than "
        "once: `would_change_with` is the list of what to search for next."
    ),
    "submit_research_record": (
        "Submit the structured record for this research task. The record is assembled from "
        "the ledger and judged against the completion contract, and the completion status "
        "that comes back is RAVEL's, not yours. INCOMPLETE with a clear list of what you "
        "could not establish is a correct answer; COMPLETE is only for a task that met "
        "every requirement."
    ),
    "read_execution_contract": (
        "Read the frozen Execution Contract for your task: the actions you may take, the "
        "ranges and substitutions permitted, the outputs you owe, and when to stop. This "
        "is the whole of your authority. An action it does not name is forbidden, not "
        "merely unmentioned."
    ),
    "read_execution_status": (
        "Read the record of what actually happened on your task: the job in flight, the "
        "record of a run that ended, what was delivered against what was required, and "
        "anything waiting on Master. Read it before reporting anything."
    ),
    "request_action": (
        "Ask whether the Execution Contract permits something — a parameter value, a "
        "substitution, an action — and be told. RAVEL answers by looking the contract up, "
        "not by judging whether the change is scientifically reasonable, and its answer is "
        "final: permitted work carries on, and anything else stops the task and asks "
        "Master. Never answer the question yourself."
    ),
    "send_message": (
        "Say one of the four permitted things to the lab: CONFIRM, INFORM, "
        "REQUEST_MISSING_INFORMATION or ESCALATE. The first three must be about something "
        "the contract names, and a message about anything else is refused — a question "
        "from an operator is escalated to Master, never answered by you, however obvious "
        "the answer looks."
    ),
}


def tools_for(role: AgentRole) -> tuple[str, ...]:
    """The tool names a role's server registers, in a stable order."""
    return tuple(name for name in sorted(TOOL_ROLES) if role in TOOL_ROLES[name])


def roles_for(tool: str) -> frozenset[AgentRole]:
    """The roles permitted to call a tool.

    Raises:
        KeyError: The tool is not part of the RAVEL roster.
    """
    return TOOL_ROLES[tool]


def mutating_tools_for(role: AgentRole) -> tuple[str, ...]:
    """The DAG-mutating tools a role holds. Master's alone, by construction."""
    return tuple(name for name in tools_for(role) if name in DAG_MUTATION_TOOLS)
