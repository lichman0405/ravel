"""Operations that span repositories and must hold as one transaction.

A repository writes one kind of record. Some operations are not one kind of
record: expanding a stage writes a Decision Record, several nodes, and the
events that announce them, and either all of that is in the database or none
of it is. Those operations live here.
"""

from ravel.state.services.dag import DagMutationService, DecisionDraft

__all__ = ["DagMutationService", "DecisionDraft"]
