"""The durable layer, expressed as Temporal workflows and activities.

The division of labour is the whole of this package:

- A **workflow** holds the shape of a run — submit, wait, retry, finish — and
  nothing else. It reads no database and touches no clock of its own, because
  Temporal replays it and any such call would happen twice.
- An **activity** does everything that touches the world: PostgreSQL, a
  backend, a clock. Each one is written to be safe to run twice, because
  Temporal will run it twice eventually.

A workflow here runs one node. It does not know what else is in the DAG, does
not decide what runs next, and cannot mutate the DAG at all: it reports what
happened to the node it was given, and Master decides what that means. That is
why `NodeRunWorkflow` is started per node with a workflow id derived from the
node — two runs of one node would be two reports of the same work, and Temporal's
own uniqueness on the id is what makes that impossible rather than unlikely.
"""
