"""What this process has opened, so that registering can be a second act.

`docs/05` §4 gives the flow as *search result → lead → open the original source
→ verify → register evidence*, and the last two are separate acts on purpose:
opening is reading, and registering is the decision that what was read is a
source for a claim. Between them the bytes have to exist somewhere, and
`ResearchSourceGateway.register` takes a `Retrieval` — the record of having
actually opened something — rather than a URL.

Across a tool call the retrieval cannot travel. Its body is bytes, and a body
that went out to a model and came back is not the body RAVEL read; a hash that
went out and came back is a hash the model typed. So the process that opened a
source keeps it, and the next call names it by a reference this process issued.

That is the whole point of this module: **it is what makes "nothing can be
registered that was not opened" true of the tool protocol and not only of the
prose.** A registration naming a reference this process never issued is refused,
because RAVEL does not hold those bytes — and the refusal says so, so the agent
knows to open it rather than to try again.

An in-process cache is not a durable store and does not pretend to be one. A
tool server that restarts loses what it had opened and not registered, and the
honest consequence is that the source has to be opened again. What must *not*
happen is the other failure: a registration that quietly reconstructs a
retrieval from the request it was given, which would write a hash for bytes
nobody checked.
"""

from __future__ import annotations

import hashlib
from collections import OrderedDict

from ravel.research.leads import Retrieval

__all__ = ["OpenedSources"]

#: How much retrieved content one process keeps. Small enough that a session
#: browsing the open web cannot grow the tool server without bound, large
#: enough that a research task's working set stays open across its calls.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: How many retrievals are kept regardless of size. A run of tiny pages —
#: DOIs resolving to abstract stubs, mostly — would otherwise be evicted by
#: count long before it hit the byte budget.
DEFAULT_MAX_ITEMS = 256


class OpenedSources:
    """The retrievals one process is holding, oldest evicted first.

    Bounded by both bytes and count because either can run away alone: a few
    large PDFs exhaust a byte budget, and a few hundred abstracts exhaust a
    count without coming near it.
    """

    def __init__(
        self,
        *,
        max_bytes: int = DEFAULT_MAX_BYTES,
        max_items: int = DEFAULT_MAX_ITEMS,
    ) -> None:
        self._held: OrderedDict[str, Retrieval] = OrderedDict()
        self._bytes = 0
        self._max_bytes = max_bytes
        self._max_items = max_items

    def remember(self, retrieval: Retrieval) -> str:
        """Hold a retrieval and return the reference that names it.

        The reference is derived from the retrieval rather than from a counter,
        so the same reading is named the same string every time it is asked
        for: a caller that lost the reference can recompute nothing, but a
        caller that remembers one from two calls ago is not naming an entry
        that has been renumbered underneath it.
        """
        reference = _reference(retrieval)
        self._forget(reference)
        self._held[reference] = retrieval
        self._bytes += _size(retrieval)
        self._evict()
        return reference

    def recall(self, reference: str) -> Retrieval | None:
        """The retrieval a reference names, if this process still holds it."""
        return self._held.get(reference)

    def forget(self, reference: str) -> None:
        """Drop one retrieval. Used when registering it, and by tests."""
        self._forget(reference)

    def clear(self) -> None:
        """Drop everything held."""
        self._held.clear()
        self._bytes = 0

    @property
    def size(self) -> int:
        """How many retrievals are held."""
        return len(self._held)

    @property
    def bytes_held(self) -> int:
        """How many bytes of retrieved content are held."""
        return self._bytes

    def _forget(self, reference: str) -> None:
        forgotten = self._held.pop(reference, None)
        if forgotten is not None:
            self._bytes -= _size(forgotten)

    def _evict(self) -> None:
        """Drop the oldest until the budget holds, never the newest.

        The entry just remembered is the one the caller opened in order to
        register it, and evicting it would fail that registration for a reason
        the agent cannot see or act on. So at least one entry is always kept,
        even when it alone exceeds the byte budget — which is reachable only
        through an explicitly small budget, since the fetcher does not hold
        bodies larger than its own inline cap.
        """
        while len(self._held) > 1 and (
            len(self._held) > self._max_items or self._bytes > self._max_bytes
        ):
            _, evicted = self._held.popitem(last=False)
            self._bytes -= _size(evicted)


def _reference(retrieval: Retrieval) -> str:
    """A stable name for one retrieval.

    Built from what the retrieval is rather than from when it was held: the URL
    that answered, the status RAVEL got, the moment it was read and the hash of
    what came back. Two openings of the same unchanged URL at different times
    are two readings and get two references, which is right — the second one is
    evidence about the source *now*, and collapsing them would lose the fact
    that somebody looked twice.
    """
    material = "|".join(
        (
            retrieval.final_url,
            retrieval.access_status.value,
            retrieval.retrieved_at.isoformat(),
            retrieval.content_hash or "",
            str(retrieval.status_code),
        )
    )
    return "ret-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:24]


def _size(retrieval: Retrieval) -> int:
    return len(retrieval.body) if retrieval.body is not None else 0
