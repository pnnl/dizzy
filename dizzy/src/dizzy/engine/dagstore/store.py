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
import os
import sqlite3
import urllib.parse
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

    def __init__(
        self, path: str = ":memory:", check_same_thread: bool = True, read_only: bool = False
    ):
        # check_same_thread=False lets a host share one store across threads;
        # the host must then serialize calls itself (sqlite3 connections are
        # not internally thread-safe).
        #
        # read_only opens the file through sqlite's `mode=ro` URI and runs no
        # schema DDL. It exists for a process that must be able to READ this
        # node's log and must not be able to touch it: the `CREATE TABLE IF NOT
        # EXISTS` below is a write transaction, so merely CONSTRUCTING a store
        # on another process's file takes that file's writer lock. A path that
        # does not exist yet cannot be opened read-only at all, and is not the
        # file this guard is protecting, so it falls through to the ordinary
        # connection.
        self.read_only = read_only and path != ":memory:" and os.path.exists(path)
        if self.read_only:
            self._db = sqlite3.connect(
                f"file:{urllib.parse.quote(path)}?mode=ro",
                uri=True,
                check_same_thread=check_same_thread,
            )
            return
        self._db = sqlite3.connect(path, check_same_thread=check_same_thread)
        self._db.executescript(_SCHEMA)
        self._db.commit()

    # ── write ────────────────────────────────────────────────────────────

    def append(self, type: str, payload: dict[str, Any]) -> Event:
        """Mint a new event on top of the current heads and store it."""
        return self.append_positioned(type, payload)[1]

    def append_positioned(self, type: str, payload: dict[str, Any]) -> tuple[int, Event]:
        """:meth:`append`, plus the row's position — the rowid `read_after` yields.

        Taken from the INSERT itself rather than recomputed afterwards, because
        the only honest source of a row's position is the statement that wrote
        it: a count taken after the transaction closes is another writer's
        answer the moment a second process shares the file, and a consumer that
        both appends and tails the log would then skip an entry it never saw.
        """
        with self._db:
            self._db.execute("BEGIN IMMEDIATE")
            event = make_event(type, self.heads(), payload)
            position = self._insert(event)
        return position, event

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

    def _insert(self, event: Event) -> int:
        """Store one event and its edges; return the row's position (rowid)."""
        cursor = self._db.execute(
            "INSERT INTO events (id, type, payload) VALUES (?, ?, ?)",
            (event.id, event.type, json.dumps(event.payload, ensure_ascii=False)),
        )
        self._db.executemany(
            "INSERT INTO edges (child, parent) VALUES (?, ?)",
            [(event.id, p) for p in event.parents],
        )
        return int(cursor.lastrowid or 0)

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

    def read_after(self, position: int = 0, limit: int = 256) -> list[tuple[int, Event]]:
        """One batch of events in LOCAL INSERTION order, after *position*.

        The position is the row's SQLite rowid. It is deliberately not the
        canonical iteration index: canonical order is a pure function of the
        event set, so a replicated event splices into the MIDDLE of it and
        renumbers everything downstream of the fork — a stored integer index
        into `iterate` is meaningless the moment a peer's fact arrives.
        Insertion order has the two properties a resumable cursor actually
        needs instead: its prefix never changes (rows are only appended), and
        it is always a valid causal order, because `append` parents on rows
        already inserted and `add` refuses an event whose parents are absent.

        The price is that it is NODE-LOCAL — two nodes holding the same events
        insert them in different orders — so a position must never be sent to
        or compared against a peer.

        Rowids are reusable in a table without AUTOINCREMENT. Nothing here
        ever deletes, so the watermark cannot be reused today — but that is a
        precondition this method rests on rather than one it can check, and
        breaking it is silent: freeing a rowid and appending hands the new
        event a position a live cursor has already passed, and that fact is
        never delivered. Anything that learns to delete an event row must
        promote this to an explicit monotonic column (AUTOINCREMENT, or a
        sequence of its own) in the same change.
        """
        rows = self._db.execute(
            "SELECT rowid, id, type, payload FROM events WHERE rowid > ? ORDER BY rowid LIMIT ?",
            (position, limit),
        ).fetchall()
        if not rows:
            return []
        ids = [r[1] for r in rows]
        parents: dict[str, list[str]] = {}
        marks = ",".join("?" * len(ids))
        for child, parent in self._db.execute(
            f"SELECT child, parent FROM edges WHERE child IN ({marks}) ORDER BY parent", ids
        ):
            parents.setdefault(child, []).append(parent)
        return [
            (
                row[0],
                Event(
                    id=row[1],
                    type=row[2],
                    parents=tuple(parents.get(row[1], ())),
                    payload=json.loads(row[3]),
                ),
            )
            for row in rows
        ]

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
