"""The DAG store: append / add / heads / iterate over SQLite.

The write method is the store's entire correctness kernel: ``append`` hashes
the payload with parents = the current local heads and inserts both atomically
inside one ``BEGIN IMMEDIATE`` transaction, so concurrent writers on one node
serialize on SQLite's writer lock — the head advance is race-free by
construction.

Heads are not maintained state; they are *derived* — an event is a head iff
no known event names it as a parent. That makes ``add`` (ingesting replicated
events) unable to corrupt the frontier: merge order can't matter because
nothing is being merged, only queried.

``iterate`` yields the canonical replay order: a deterministic topological
sort — parents before children, concurrent siblings tiebroken by id. Any two
stores holding the same event set iterate identically; that property is the
convergence invariant the Hypothesis suite pins down.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from typing import Any

from dizzy.engine.dagstore.events import Event, TamperedEvent, make_event, verify

__all__ = ["DagStore", "MissingParents"]


class MissingParents(KeyError):
    """add() was given an event whose parents are not yet in the store."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id      TEXT PRIMARY KEY,
    type    TEXT NOT NULL,
    payload TEXT NOT NULL          -- canonical JSON
);
CREATE TABLE IF NOT EXISTS edges (
    child   TEXT NOT NULL REFERENCES events(id),
    parent  TEXT NOT NULL,
    PRIMARY KEY (child, parent)
);
CREATE INDEX IF NOT EXISTS edges_by_parent ON edges(parent);
"""


class DagStore:
    """A single node's event DAG. ``:memory:`` (default) or a file path."""

    def __init__(self, path: str = ":memory:", check_same_thread: bool = True):
        # check_same_thread=False lets a host share one store across threads;
        # the host must then serialize calls itself (sqlite3 connections are
        # not internally thread-safe).
        self._db = sqlite3.connect(path, check_same_thread=check_same_thread)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ── write ────────────────────────────────────────────────────────────

    def append(self, type: str, payload: dict[str, Any]) -> Event:
        """Mint a new event on top of the current heads and store it."""
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            event = make_event(type, self.heads(), payload)
            self._insert(event)
        return event

    def add(self, event: Event) -> bool:
        """Ingest a replicated event. Returns False if already present.

        Verifies the content hash (raises TamperedEvent) and requires every
        parent to be present already (raises MissingParents) — replication
        must deliver ancestry first, which `sync` guarantees.
        """
        if not verify(event):
            raise TamperedEvent(f"id {event.id} does not match content")
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            if event.id in self:
                return False
            missing = [p for p in event.parents if p not in self]
            if missing:
                raise MissingParents(missing)
            self._insert(event)
        return True

    def _insert(self, event: Event) -> None:
        self._db.execute(
            "INSERT INTO events (id, type, payload) VALUES (?, ?, ?)",
            (event.id, event.type, json.dumps(event.payload, ensure_ascii=False)),
        )
        self._db.executemany(
            "INSERT INTO edges (child, parent) VALUES (?, ?)",
            [(event.id, p) for p in event.parents],
        )

    # ── read ─────────────────────────────────────────────────────────────

    def heads(self) -> tuple[str, ...]:
        """The DAG frontier (sorted): events no known event names as parent."""
        rows = self._db.execute(
            "SELECT id FROM events WHERE id NOT IN (SELECT parent FROM edges) ORDER BY id"
        ).fetchall()
        return tuple(r[0] for r in rows)

    def get(self, event_id: str) -> Event:
        row = self._db.execute(
            "SELECT id, type, payload FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            raise KeyError(event_id)
        parents = tuple(
            r[0]
            for r in self._db.execute(
                "SELECT parent FROM edges WHERE child = ? ORDER BY parent", (event_id,)
            )
        )
        return Event(id=row[0], type=row[1], parents=parents, payload=json.loads(row[2]))

    def __contains__(self, event_id: str) -> bool:
        return (
            self._db.execute("SELECT 1 FROM events WHERE id = ?", (event_id,)).fetchone()
            is not None
        )

    def __len__(self) -> int:
        return self._db.execute("SELECT count(*) FROM events").fetchone()[0]

    def ids(self) -> frozenset[str]:
        return frozenset(r[0] for r in self._db.execute("SELECT id FROM events"))

    def iterate(self) -> Iterator[Event]:
        """Yield every event in canonical replay order.

        Kahn's algorithm with the ready set kept as a sorted frontier: an
        event becomes ready once all its parents have been emitted; among
        ready events the smallest id goes first. Purely a function of the
        event set — no clocks, no insertion order.
        """
        import heapq

        pending: dict[str, int] = {}  # id -> unemitted parent count
        children: dict[str, list[str]] = {}
        for child, parent in self._db.execute("SELECT child, parent FROM edges"):
            pending[child] = pending.get(child, 0) + 1
            children.setdefault(parent, []).append(child)

        ready = [r[0] for r in self._db.execute("SELECT id FROM events") if r[0] not in pending]
        heapq.heapify(ready)
        emitted = 0
        while ready:
            event_id = heapq.heappop(ready)
            yield self.get(event_id)
            emitted += 1
            for child in children.get(event_id, ()):
                pending[child] -= 1
                if pending[child] == 0:
                    del pending[child]
                    heapq.heappush(ready, child)
        if pending:
            raise RuntimeError(f"DAG has {len(pending)} events with unresolvable parents")
