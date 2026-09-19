"""Durable execution: the layer between a node and whatever actually runs it.

Nothing here owns the Scientific DAG. A workflow in this package runs one node
and reports what happened to it; the DAG's shape, the contracts, and the
decision to run anything at all are Master's, and they are already in
PostgreSQL before a workflow starts.

The two files that matter:

- `backends.py` is the port. Compute and experiment differ in their vocabulary
  and agree on the durable subset, which is what the workflow is written
  against — so a new backend is a new class, not a new workflow.
- `policies.py` holds the two decisions the durable layer makes on its own:
  whether to try again, and how long to wait. Both are pure functions of the
  contract and the job record, so both are testable without Temporal.
"""
