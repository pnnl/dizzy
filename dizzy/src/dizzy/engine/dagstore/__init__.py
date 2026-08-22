"""dagstore — the content-addressed event DAG the event store is built on.

Cryptographic event ids, parent pointers for order, heads as checkpoint,
canonical topological replay, git-style anti-entropy sync. Stdlib only: this
subpackage adds nothing to DIZZY's dependency footprint, which is why it can
sit in core alongside the shells rather than behind an extra.

It knows nothing about DIZZY — no feat file, no generated classes, not even
pydantic. It stores ``(type: str, payload: dict)`` and returns hashed
``Event`` records. :mod:`dizzy.engine.store` is the layer that gives those
payloads their DIZZY meaning.
"""

from dizzy.engine.dagstore.canonical import NotCanonicalizable, canonical_json
from dizzy.engine.dagstore.events import (
    Event,
    TamperedEvent,
    compute_id,
    make_event,
    verify,
)
from dizzy.engine.dagstore.store import DagStore, MissingParents
from dizzy.engine.dagstore.sync import sync

__all__ = [
    "DagStore",
    "Event",
    "MissingParents",
    "NotCanonicalizable",
    "TamperedEvent",
    "canonical_json",
    "compute_id",
    "make_event",
    "sync",
    "verify",
]
