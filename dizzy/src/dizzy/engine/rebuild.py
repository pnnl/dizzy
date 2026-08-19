"""Replay — truncate the read models and refold the whole event stream.

The recoverability test, and the reason an event-sourced system can treat its
read models as a cache: the stream is the truth, so every model row must be
reconstructible from it. :func:`rebuild` drops and recreates every model table,
then folds each envelope through the projections registered for its event type,
reusing the envelope's ``ingested_at`` — so both time axes survive a rebuild
and only the fold wall-clock changes.

The algorithm is feature-agnostic, which is the whole point of it living here:
*which* projections fold an event, and *which* tables exist, are the wiring's
knowledge and arrive as arguments. Nothing in this module imports an ORM —
``session`` and ``metadatas`` are duck-typed, so a host on something other than
SQLAlchemy can still use it. The SQLAlchemy conveniences that most hosts want
alongside it live in :mod:`dizzy.engine.sqla`, behind the ``sqla`` extra.

A retired event type — a fact in the stream that no current projection folds —
is skipped, not fatal. The stream is append-only; the feature that read it may
be gone.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable, Mapping
from typing import Any

from dizzy.engine.store import EventStore


def rebuild(
    store: EventStore,
    session: Any,
    runners: Mapping[Any, list],
    metadatas: Iterable[Any],
    report: Any = sys.stderr,
) -> int:
    """Truncate all models, then refold the stream. Returns events folded.

    *runners* maps event class -> ``[(name, runner)]``, the shape the engine
    registers; *metadatas* is the collection of table metadata to drop and
    recreate. Pass ``report=None`` to silence the retired-type notice.
    """
    bind = session.get_bind()
    metadatas = list(metadatas)
    for md in metadatas:
        md.drop_all(bind)
    for md in metadatas:
        md.create_all(bind)

    folded = 0
    skipped: dict[str, int] = {}
    for envelope in store.iterate():
        try:
            event = store.reconstruct_event(envelope)
        except KeyError:
            # Retired event type: the fact stays in the stream (append-only),
            # but no current feature folds it. Skip, don't fail the replay.
            skipped[envelope.type] = skipped.get(envelope.type, 0) + 1
            continue
        for _name, runner in runners.get(type(event), []):
            runner(event, envelope.ingested_at)
        folded += 1
    session.commit()
    if skipped and report is not None:
        print(f"[rebuild] skipped retired event types: {skipped}", file=report)
    return folded
