"""The event store — the truth an engine appends to before anything else runs.

A content-addressed DAG (:mod:`dizzy.engine.dagstore`) behind an
append/iterate interface. Each append becomes a dagstore event whose hashed
payload is ``{"ingested_at": <utc iso>, "event": <domain payload>}`` —
``ingested_at`` is stamped once, at first append, and travels with the fact
(fold-on-replicate reuses it, so both time axes survive). Identity is the
content hash; order is parent pointers; iteration is canonical topological
order.

``seq`` is not stored. The :class:`Envelope` still carries one as a *derived*
iteration index — this node's chain is linear, so topological order equals
append order — which keeps "events since N" consumers working.

Floats in payloads are stringified before hashing: the canonical form forbids
them for cross-language stability, and pydantic's lax coercion restores them
on reconstruct (``float(repr(x)) == x`` exactly).

**What makes this feature-agnostic.** The store needs to map a stored event's
type name back to a class to rehydrate it, and that map comes from the feat —
:attr:`FeatGraph.events <dizzy.engine.registry.FeatGraph.events>`, not a scan
of the generated events module. The resolution is lazy, so a process that only
appends (a worker draining a queue) never imports the generated definitions
package at all.

Cross-thread: one shared DagStore connection guarded by a lock.
Cross-process: DagStore's ``BEGIN IMMEDIATE`` serializes writers on the node.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dizzy.engine.dagstore import DagStore
from dizzy.engine.registry import FeatGraph, snake_case
from dizzy.engine.registry import graph as default_graph

DEFAULT_STORE_PATH = Path("data") / "events.db"
"""Where the stream lands when neither an argument nor ``$DIZZY_STORE_PATH``
says otherwise. Relative to the process's cwd — a host that runs from
anywhere should pass a path or set the variable."""


def _stringify_floats(value: Any) -> Any:
    """repr-stringify floats so the payload fits the canonical (hashable)
    subset; pydantic lax mode coerces them back losslessly on reconstruct."""
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, list):
        return [_stringify_floats(v) for v in value]
    if isinstance(value, dict):
        return {k: _stringify_floats(v) for k, v in value.items()}
    return value


@dataclass
class Envelope:
    """One appended fact, as the stream knows it."""

    id: str
    """Content hash — the event's stream identity."""
    type: str
    """Event name in snake_case, i.e. the name the feat declares."""
    ingested_at: datetime
    """UTC, stamped at first append."""
    payload: dict
    parents: tuple = ()
    seq: int = -1
    """DERIVED iteration index, not stored."""


def reconstruct_event(envelope: Envelope, event_classes: Mapping[str, type]) -> Any:
    """Rebuild the event instance an envelope stands for.

    *event_classes* is a feat-name -> class map; ``FeatGraph.events`` is one.
    """
    cls = event_classes.get(envelope.type)
    if cls is None:
        raise KeyError(
            f"unknown event type in stream: {envelope.type!r} — the feat does "
            f"not declare it, so this stream was written by a different "
            f"feature (or by a newer version of this one)"
        )
    return cls(**envelope.payload)


class EventStore:
    """Content-addressed event store. Path from arg > ``$DIZZY_STORE_PATH`` > default."""

    def __init__(
        self,
        path: str | Path | None = None,
        event_classes: Mapping[str, type] | None = None,
        graph: FeatGraph | None = None,
    ):
        """*event_classes* maps feat event name -> class, for
        :meth:`reconstruct_event`. Omit both it and *graph* and the store reads
        the ambient feat file when (and only when) something first reconstructs.
        """
        if path is None:
            path = os.environ.get("DIZZY_STORE_PATH") or DEFAULT_STORE_PATH
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self.dag = DagStore(str(self.path), check_same_thread=False)
        self._event_classes = dict(event_classes) if event_classes is not None else None
        self._graph = graph

    @property
    def event_classes(self) -> Mapping[str, type]:
        """The feat's event map, resolved on first use.

        Deferred because appending needs no classes: a worker that only writes
        the stream should not pay to import the generated definitions package.
        """
        if self._event_classes is None:
            self._event_classes = dict((self._graph or default_graph()).events)
        return self._event_classes

    def append(self, event: Any, ingested_at: datetime | None = None) -> Envelope:
        """Append one event and return its envelope.

        ``ingested_at`` is stamped NOW unless supplied — a caller supplies it
        only when replaying or replicating an already-stamped fact.
        """
        stamped = ingested_at or datetime.now(UTC)
        wrapped = {
            "ingested_at": stamped.isoformat(),
            "event": _stringify_floats(event.model_dump(mode="json")),
        }
        with self._lock:
            dag_event = self.dag.append(snake_case(type(event).__name__), wrapped)
            seq = len(self.dag) - 1
        return Envelope(
            id=dag_event.id,
            type=dag_event.type,
            ingested_at=stamped,
            payload=wrapped["event"],
            parents=dag_event.parents,
            seq=seq,
        )

    def iterate(self) -> Iterator[Envelope]:
        """Yield all envelopes in canonical (topological) order."""
        with self._lock:
            events = list(self.dag.iterate())
        for i, ev in enumerate(events):
            yield Envelope(
                id=ev.id,
                type=ev.type,
                ingested_at=datetime.fromisoformat(ev.payload["ingested_at"]),
                payload=ev.payload["event"],
                parents=ev.parents,
                seq=i,
            )

    def heads(self) -> tuple:
        with self._lock:
            return self.dag.heads()

    # ── Replication surface ─────────────────────────────────────────────────
    #
    # Replicated facts arrive already hashed and already stamped, so they do
    # not go through append(): they are ADDED, keeping the id the peer minted.

    def add_replicated(self, event: Any) -> Envelope:
        """Ingest a dagstore event fetched from a peer, returning its envelope.

        The hash is verified on arrival by the DAG, and every parent must
        already be present — replication delivers ancestry first.
        """
        with self._lock:
            self.dag.add(event)
        return Envelope(
            id=event.id,
            type=event.type,
            ingested_at=datetime.fromisoformat(event.payload["ingested_at"]),
            payload=event.payload["event"],
            parents=event.parents,
        )

    def raw_event(self, event_id: str) -> Any:
        """The stored dagstore event, wrapper payload and all — what a peer
        asks for over the wire. Raises KeyError if absent."""
        with self._lock:
            return self.dag.get(event_id)

    def __contains__(self, event_id: str) -> bool:
        with self._lock:
            return event_id in self.dag

    def __len__(self) -> int:
        with self._lock:
            return len(self.dag)

    def reconstruct_event(self, envelope: Envelope) -> Any:
        """Rebuild the event instance from an envelope."""
        return reconstruct_event(envelope, self.event_classes)
