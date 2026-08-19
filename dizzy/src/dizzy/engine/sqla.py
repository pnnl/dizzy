"""SQLAlchemy conveniences for hosts whose read models are a SQLite cache.

Optional, and deliberately kept out of the engine: :mod:`dizzy.engine.loop`
and :mod:`dizzy.engine.rebuild` touch read models only through the runners a
wiring registers, so DIZZY's runtime carries no ORM. This module is for the
common case rather than the required one, and lives behind the ``sqla`` extra.

What it provides is one idea: **the read model is a disposable cache, and the
stream is the truth**, so schema drift is not a migration problem. A model that
gains a column does not need an ALTER — it needs a refold. That makes one
mechanism handle any schema change, and exercises the recoverability path
instead of routing around it.

Freshness is judged by a COMPLETION MARKER (a file holding the schema
fingerprint, written only after a successful refold), never by inspecting
tables: a crashed rebuild leaves every table present-but-empty, which table
inspection happily calls current. A missing or stale marker keeps retriggering
the rebuild until one finishes.

Concurrency: a server and every worker run :func:`ensure_current` at startup,
so a file lock serializes them and late arrivals re-check the marker under the
lock and find the work already done. The lock and marker sit next to the
engine's own database file, so a test on a tmp path never touches real data.
The lock uses ``fcntl`` and is therefore POSIX-only; on other platforms call
:func:`refold_if_stale` yourself under whatever mutual exclusion you have.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("data") / "models.db"
DB_PATH_ENV = "DIZZY_DB_PATH"


def db_path(path: str | Path | None = None) -> Path:
    """Read-model database path: argument > ``$DIZZY_DB_PATH`` > default."""
    return Path(path or os.environ.get(DB_PATH_ENV) or DEFAULT_DB_PATH)


def make_engine(path: str | Path | None = None) -> Any:
    """A SQLite engine tuned for N workers folding into one file.

    WAL lets readers proceed under a writer, and ``busy_timeout`` queues
    writers instead of raising "database is locked" — both of which the mp
    shell needs and the st shell is indifferent to.
    """
    from sqlalchemy import create_engine
    from sqlalchemy import event as sqla_event

    resolved = db_path(path)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{resolved}")

    @sqla_event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):
        cursor = dbapi_conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    return engine


def schema_fingerprint(metadatas: Iterable[Any]) -> str:
    """Deterministic digest of every model table's column set."""
    cols = sorted(
        f"{t.name}.{c.name}" for md in metadatas for t in md.tables.values() for c in t.columns
    )
    return hashlib.sha256("\n".join(cols).encode()).hexdigest()


def create_all(sqla_engine: Any, metadatas: Iterable[Any]) -> None:
    for md in metadatas:
        md.create_all(sqla_engine)


def drop_all(sqla_engine: Any, metadatas: Iterable[Any]) -> None:
    for md in metadatas:
        md.drop_all(sqla_engine)


def marker_path(sqla_engine: Any) -> Path:
    """Where the completion marker for this engine's database lives."""
    return Path(sqla_engine.url.database).with_suffix(".schema")


def _read_marker(marker: Path) -> str | None:
    try:
        return marker.read_text().strip()
    except OSError:
        return None


def refold_if_stale(
    sqla_engine: Any,
    metadatas: Iterable[Any],
    refold: Callable[[Any], int],
    report: Any = sys.stderr,
) -> int | None:
    """Refold the stream if the marker does not match the model schema.

    *refold* receives a session and returns the number of events folded —
    ``lambda session: rebuild(store, session, runners, metadatas)`` is the
    usual choice. Returns that count, or ``None`` if the cache was already
    current. Caller provides mutual exclusion; see :func:`ensure_current`.
    """
    from sqlalchemy.orm import Session

    metadatas = list(metadatas)
    marker = marker_path(sqla_engine)
    fingerprint = schema_fingerprint(metadatas)
    if _read_marker(marker) == fingerprint:
        return None
    if report is not None:
        print(
            "[rebuild] model schema changed — refolding the stream "
            "(the read model is a cache; the stream is the truth)",
            file=report,
        )
    with Session(sqla_engine) as session:
        folded = refold(session)
    marker.write_text(fingerprint)  # only a FINISHED refold counts
    if report is not None:
        print(f"[rebuild] refolded {folded} events", file=report)
    return folded


def ensure_current(
    sqla_engine: Any,
    metadatas: Iterable[Any],
    refold: Callable[[Any], int],
    report: Any = sys.stderr,
) -> int | None:
    """Create missing tables, then heal schema drift by REBUILD, not ALTER.

    Safe to call from every process at startup: a file lock serializes them,
    and whoever loses re-checks the marker and returns immediately.
    """
    import fcntl

    metadatas = list(metadatas)
    create_all(sqla_engine, metadatas)
    marker = marker_path(sqla_engine)
    if _read_marker(marker) == schema_fingerprint(metadatas):
        return None
    with open(marker.with_suffix(".rebuild.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return refold_if_stale(sqla_engine, metadatas, refold, report=report)
