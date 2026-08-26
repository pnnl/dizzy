"""Cursor stores — where a consumer group's position in the event log lives.

A cursor is a group's answer to "where had I got to?", so that a boot is
snapshot+tail rather than replay-from-zero. topology.yaml names the kinds
(``memory``, ``sqlite``, ``postgres``) and states the contract this module is
built to:

    Durable so a boot is snapshot+tail rather than replay-from-zero, but
    DISPOSABLE: losing one must be safe, not merely recoverable.

That second half is the whole design, and it is what keeps a cursor a
performance checkpoint instead of a second source of truth. Concretely: the
only thing a cursor may ever do is let a consumer SKIP a prefix of the log it
has already folded. Deleting every cursor must leave a system that reaches
the same state, more slowly. Nothing here may hold state that the event log
does not already hold, and no consumer may treat a missing cursor as an error
— :meth:`CursorStore.load` returns :data:`START` for a group it has never
seen, and that is a normal, correct answer, not a fault. The suite proves it
rather than asserting it in prose: ``test_cursors.py`` folds a stream with a
cursor, throws the store away — rewinding the in-memory kind, unlinking the
sqlite file — replays from zero, and compares the states.

**What a position is.** An opaque, monotone watermark handed out by the event
store — today :attr:`Envelope.position <dizzy.engine.store.Envelope.position>`,
a watermark in this node's INSERTION order. This module never interprets it
beyond "bigger is later", because that is all a cursor needs and all the store
promises. Two properties come with it, and they are the store's to explain,
not this module's to enforce: a position is NODE-LOCAL (never replicate one,
never compare one across peers) and it is NOT an index into canonical order
(``Envelope.seq`` is a pass-local derivation and must never be saved).

**Crash safety — the caller's obligation.** A store cannot make this safe
alone, so state it plainly: *commit the work, then save the position.* The
projection's read-model write and any effect a group performs must be durable
BEFORE :meth:`CursorStore.save` names the event that produced them. Save
first and a crash in between loses work silently — the cursor claims ground
the system never covered. Save second and the same crash re-delivers events
already folded, which the system is built to absorb: topology.yaml's
disposability requirement already demands confluent, idempotent folds, and
this is the case that spends them. At-least-once in that direction; never
at-most-once. :meth:`save` is monotone for the same reason — see its
docstring.

**Nothing app-specific.** Per the engine tree's standing rule, this file names
no command, event, or environment field. It takes group names and integers.
The group names come from the topology, which is the only place that knows
them; a row for a group the topology no longer declares is inert (nothing
reads it) and is removed by an explicit :meth:`prune`, never by a guess made
here about which groups ought to exist.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import Protocol, runtime_checkable

DEFAULT_CURSOR_PATH = Path("data") / "cursors.db"
"""Where sqlite cursors land when neither the topology's ``url`` nor
``$DIZZY_CURSOR_PATH`` says otherwise — beside the event store, per
topology.yaml's description of the kind. Relative to the process's cwd."""

START = 0
"""The position of a group that has folded nothing. What :meth:`load` returns
for an unknown group, and the value a consumer passes to
:meth:`EventStore.iterate_from <dizzy.engine.store.EventStore.iterate_from>`
to replay the whole stream. Deliberately not ``None``: "I have no cursor" and
"start at the beginning" are the same instruction, and code that has to branch
on them will eventually branch wrongly."""


@runtime_checkable
class CursorStore(Protocol):
    """The seam every cursor kind implements.

    Small on purpose. A cursor is one integer per group; anything richer
    (leases, ownership, in-flight sets) belongs to the transport, which is
    allowed to lose things, and not to the log position, which is not.

    This Protocol is also the extension point topology.yaml's third kind
    needs: a ``PostgresCursorStore`` is these six methods over a table with
    ``group_name`` as the primary key, and :func:`open_cursor_store` grows one
    branch. It is NOT implemented here — psycopg is not a dependency of this
    project and adding one is out of scope. :func:`open_cursor_store` says so
    when asked for it rather than pretending.
    """

    def load(self, group: str) -> int:
        """This group's saved position, or :data:`START` if it has none."""
        ...

    def save(self, group: str, position: int) -> int:
        """Record that *group* has finished everything up to *position*."""
        ...

    def groups(self) -> dict[str, int]:
        """Every group this store holds a cursor for, position included."""
        ...

    def rewind(self, group: str, position: int = START) -> None:
        """Force a position backwards — the deliberate replay."""
        ...

    def prune(self, keep: Collection[str]) -> tuple[str, ...]:
        """Drop cursors for groups not in *keep*; return what was dropped."""
        ...

    def close(self) -> None: ...


def _validate(group: str, position: int) -> None:
    if not group:
        raise ValueError("a cursor needs a group name")
    if position < START:
        raise ValueError(
            f"position {position} is before the start of the log; a cursor is a "
            f"watermark, and there is nothing earlier than {START}"
        )


def _as_path(value: str | Path) -> str | Path:
    """Accept topology's ``url`` in either form it documents.

    topology.yaml calls the field "Connection string or path", so
    ``sqlite:///data/cursors.db`` is a declared-legal value and must not be
    handed to :class:`Path` verbatim — that silently creates a directory
    literally named ``sqlite:`` under the process cwd, and every boot from a
    different cwd then finds no cursor and replays from zero while looking
    healthy. Slashes follow the usual convention: the authority is empty, so
    ``sqlite:///x.db`` is relative and ``sqlite:////x.db`` is absolute. Any
    other scheme is a configuration mistake and says so.
    """
    text = str(value)
    if "://" not in text:
        return value
    scheme, _, rest = text.partition("://")
    if scheme != "sqlite":
        raise ValueError(
            f"{text!r} is not a sqlite location: pass a filesystem path, or a "
            "'sqlite://' url (sqlite:///relative.db, sqlite:////absolute.db)"
        )
    return rest[1:] if rest.startswith("/") else rest


class MemoryCursorStore:
    """Cursors in a dict — correct, and durable for exactly one process.

    topology.yaml's own words: "every boot replays from zero, which is correct
    but unbounded in cost as the log grows." That is this class in one line,
    and it is the reason it is worth shipping: it is the executable proof that
    losing every cursor costs time and never correctness. If a test passes
    against :class:`SqliteCursorStore` and fails against this one, the
    difference is a consumer treating its cursor as truth.

    Thread-safe within the process. Deliberately NOT shared between processes:
    two workers on one group each keeping their own private idea of the
    position is a correctness question, and answering it silently is worse
    than the ``sqlite`` kind, which answers it for real.
    """

    def __init__(self, initial: Iterable[tuple[str, int]] = ()):
        self._positions: dict[str, int] = {}
        self._lock = threading.Lock()
        for group, position in initial:
            self.save(group, position)

    def load(self, group: str) -> int:
        with self._lock:
            return self._positions.get(group, START)

    def save(self, group: str, position: int) -> int:
        _validate(group, position)
        with self._lock:
            current = self._positions.get(group, START)
            new = max(current, position)
            self._positions[group] = new
            return new

    def groups(self) -> dict[str, int]:
        with self._lock:
            return dict(self._positions)

    def rewind(self, group: str, position: int = START) -> None:
        _validate(group, position)
        with self._lock:
            self._positions[group] = position

    def prune(self, keep: Collection[str]) -> tuple[str, ...]:
        keep = set(keep)
        with self._lock:
            dropped = tuple(sorted(g for g in self._positions if g not in keep))
            for group in dropped:
                del self._positions[group]
        return dropped

    def close(self) -> None:
        """Nothing to release. Present so callers need not know the kind."""


_SCHEMA = """
CREATE TABLE IF NOT EXISTS cursors (
    group_name  TEXT PRIMARY KEY,
    position    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


class SqliteCursorStore:
    """Cursors in a SQLite file of their own.

    Its own file, like ``command_queue.db``, and for a sharper reason: this
    one is the DISPOSABLE database. Putting cursors in the event store's file
    would make ``rm`` a data-loss operation instead of a slow boot, and would
    tempt a future reader into a transaction spanning facts and positions —
    which is precisely the coupling that turns a checkpoint into a second
    source of truth.

    Disposable means the FILE, thrown away while nothing holds it open —
    ``rm`` it with the processes down, or call :meth:`prune` / :meth:`rewind`
    on a live store. Unlinking a database another connection still holds
    leaves that connection's WAL sidecar for the replacement to adopt, and
    SQLite reports that as an I/O error rather than as an empty store; the
    disposability argument is about what a cursor MEANS, and it cannot repeal
    POSIX.

    WAL, matching ``st/command_queue.py``: readers never block the writer, and
    the file survives a killed process without a recovery pass of its own.
    That plus ``busy_timeout`` is what makes multi-process access work — see
    :meth:`save` for what is and is not guaranteed when two workers share a
    group.
    """

    def __init__(self, path: str | Path | None = None):
        if path is None:
            path = os.environ.get("DIZZY_CURSOR_PATH") or DEFAULT_CURSOR_PATH
        self.path = Path(_as_path(path))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        # A writer waits for a peer's commit instead of raising: cursor writes
        # are short single statements, so contention resolves in microseconds
        # and the alternative — a worker crashing on a saved position — throws
        # away the one thing this file exists to avoid.
        self._db.execute("PRAGMA busy_timeout=5000")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self.recovered = self._recover()

    def _recover(self) -> tuple[str, ...]:
        """Rewind any cursor that is not a usable watermark, and say which.

        A row this code could not have written — an interrupted migration, a
        hand-edited file, an older schema — is repaired rather than obeyed.
        There is exactly one safe repair and disposability is what makes it
        safe: rewind to :data:`START` and re-fold. A cursor is never worth
        guessing at, because guessing forward skips work that no other record
        can recover, while guessing backward costs a replay.

        The test is on the storage class, not the value, because SQLite's
        ``INTEGER`` is an affinity and not a constraint: a hand edit can leave
        a TEXT position, and TEXT sorts above every integer, so a value check
        alone would let one through — and then every method here raises on the
        ``int()``, taking :meth:`groups` and :meth:`prune` down with the one
        broken group.

        What this cannot catch is a position PAST the end of the log, because
        nothing in this module has ever seen the log; that repair belongs to a
        caller holding both, and today the tool for it is :meth:`rewind`.
        """
        bad = "typeof(position) <> 'integer' OR position < ?"
        with self._lock:
            rows = self._db.execute(
                f"SELECT group_name FROM cursors WHERE {bad}", (START,)
            ).fetchall()
            if rows:
                self._db.execute(f"UPDATE cursors SET position=? WHERE {bad}", (START, START))
                self._db.commit()
            return tuple(sorted(r["group_name"] for r in rows))

    def load(self, group: str) -> int:
        with self._lock:
            row = self._db.execute(
                "SELECT position FROM cursors WHERE group_name=?", (group,)
            ).fetchone()
        return START if row is None else int(row["position"])

    def save(self, group: str, position: int) -> int:
        """Advance *group* to *position*; return the position now stored.

        MONOTONE: a save that would move a cursor backwards is ignored, and
        the stored (higher) position is returned. Two reasons, both from the
        contract. First, the crash rule above means saves trail committed
        work, so a late save carrying a stale position is an ordinary race,
        not an error — and letting it win would re-deliver work with no one
        having asked. Second, a cursor is a watermark, and a watermark that
        can move backwards on its own is not one.

        What monotonicity does NOT do is make ``workers: N`` free at this
        seam. One cursor is one lane's watermark: if two workers on a group
        split the stream between them, the leader's save covers a prefix the
        laggard has not committed, and a crash there loses that work — the
        at-most-once this module exists to forbid. A group's workers must
        therefore either all fold the same positions in order (one lane, N
        workers is then a transport question about who holds the doorbell) or
        hold cursors of their own. Partitioning a group's stream across
        workers under one cursor is not a scaling decision, and nothing here
        can make it one.

        Deliberate replay is :meth:`rewind`, which is loud about it.

        Done in a single upserting statement so it is atomic across
        connections without a transaction: the ``max`` is evaluated by SQLite
        under the row's write lock, which a read-then-write in Python could
        not promise between two processes.
        """
        _validate(group, position)
        with self._lock:
            self._db.execute(
                "INSERT INTO cursors (group_name, position, updated_at)"
                " VALUES (?, ?, datetime('now'))"
                " ON CONFLICT(group_name) DO UPDATE SET"
                "  position=max(cursors.position, excluded.position),"
                "  updated_at=excluded.updated_at",
                (group, position),
            )
            self._db.commit()
            row = self._db.execute(
                "SELECT position FROM cursors WHERE group_name=?", (group,)
            ).fetchone()
        return int(row["position"])

    def groups(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT group_name, position FROM cursors").fetchall()
        return {r["group_name"]: int(r["position"]) for r in rows}

    def rewind(self, group: str, position: int = START) -> None:
        """Move a cursor backwards on purpose — an operator re-folding a group.

        Separate from :meth:`save` because the two are opposite intentions and
        a single method taking both would make the dangerous one reachable by
        an off-by-one in a worker.
        """
        _validate(group, position)
        with self._lock:
            self._db.execute(
                "INSERT INTO cursors (group_name, position, updated_at)"
                " VALUES (?, ?, datetime('now'))"
                " ON CONFLICT(group_name) DO UPDATE SET"
                "  position=excluded.position, updated_at=excluded.updated_at",
                (group, position),
            )
            self._db.commit()

    def prune(self, keep: Collection[str]) -> tuple[str, ...]:
        """Drop cursors for groups not in *keep*; return what was dropped.

        A renamed or deleted group leaves a row behind, and that row is inert
        — nothing loads a group the topology does not declare, so a stale
        cursor costs a few bytes and cannot affect another group. It is
        removed only when a caller that actually knows the current topology
        asks, because this module does not and must not: guessing that an
        unrecognised name is obsolete would delete a live group's cursor
        during a rolling deploy, when the new topology has not reached every
        host yet.
        """
        stale = tuple(sorted(set(self.groups()) - set(keep)))
        if stale:
            with self._lock:
                self._db.executemany(
                    "DELETE FROM cursors WHERE group_name=?", [(g,) for g in stale]
                )
                self._db.commit()
        return stale

    def close(self) -> None:
        with self._lock:
            self._db.close()


def open_cursor_store(kind: str = "memory", url: str | None = None) -> CursorStore:
    """Build the cursor store a topology's ``cursors:`` section asks for.

    *kind* and *url* are topology.yaml's two fields, taken as plain strings so
    that nothing in the engine tree has to import the generated topology
    schema to reach a store.
    """
    if kind == "memory":
        return MemoryCursorStore()
    if kind == "sqlite":
        return SqliteCursorStore(url)
    if kind == "postgres":
        raise NotImplementedError(
            "the postgres cursor store is declared in topology.yaml but not "
            "implemented: this project depends on pyyaml and pydantic, and a "
            "postgres driver is not among its extras. Implement CursorStore "
            "over a cursors table — the Protocol is the whole contract — in a "
            "package that owns the driver dependency."
        )
    raise ValueError(
        f"unknown cursor store kind {kind!r}; topology.yaml declares memory, sqlite, postgres"
    )
