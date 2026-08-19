"""Anti-entropy sync: the stop-at-known walk, node-to-node.

``sync(dst, src)`` pulls into *dst* everything *src* has that *dst* lacks:
start from src's heads, walk parent pointers backwards, stop at anything dst
already holds (exactly the feat's incremental-ingest idiom), then deliver the
fetched subset parents-first. Content hashes make every fetched record
verifiable on arrival (``add`` recomputes them).
"""

from __future__ import annotations

from dizzy.engine.dagstore.events import Event
from dizzy.engine.dagstore.store import DagStore

__all__ = ["sync"]


def sync(dst: DagStore, src: DagStore) -> int:
    """Pull src's missing ancestry into dst. Returns events transferred."""
    fetched: dict[str, Event] = {}
    frontier = [h for h in src.heads() if h not in dst]
    while frontier:
        event_id = frontier.pop()
        if event_id in fetched or event_id in dst:
            continue
        event = src.get(event_id)
        fetched[event_id] = event
        frontier.extend(event.parents)

    # Deliver parents-first: topo-sort the fetched subset (a parent outside
    # the subset is already in dst, so it doesn't gate readiness).
    pending = dict(fetched)
    while pending:
        ready = [e for e in pending.values() if all(p not in pending for p in e.parents)]
        if not ready:
            raise RuntimeError("cycle in fetched subset — hashes forbid this")
        for event in sorted(ready, key=lambda e: e.id):
            dst.add(event)
            del pending[event.id]
    return len(fetched)
