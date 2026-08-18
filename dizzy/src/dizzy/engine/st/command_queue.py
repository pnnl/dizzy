"""Durable background command queue — SQLite-backed replacement for queue.Queue.

The engine's two-queue control loop needs an external COMMAND queue with a
``.put(command)`` interface (engine.py dispatches policy cascades onto it).
The old in-memory queue.Queue lost everything on restart — this one persists
each command as a row in its own SQLite file (default ./data/command_queue.db,
override $DIZZY_COMMAND_QUEUE_PATH) so queued work survives crashes and
deploys, and the host can answer "what are we waiting on?".

Deliberately its own DB file: models.db is disposable (rebuilt from the event
store) and the event store holds facts, not intent. Commands here are intent.

Semantics are at-least-once: rows stuck in ``running`` at startup are
recovered to ``queued`` and re-executed. That is safe for this system —
record_media dedups on blob_hash and re-classifying an image is only a wasted
LLM call.

State transitions fan out to in-process subscribers (one queue.Queue each)
which back the /api/command-queue/stream SSE endpoint.
"""

from __future__ import annotations

import os
import queue
import sqlite3
import threading
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_QUEUE_PATH = "./data/command_queue.db"  # host override: $DIZZY_COMMAND_QUEUE_PATH

# How many finished (done/error) rows to keep as history.
KEEP_FINISHED = 2000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    command_type  TEXT NOT NULL,
    payload_json  TEXT NOT NULL,
    label         TEXT NOT NULL DEFAULT '',
    origin        TEXT NOT NULL DEFAULT '',
    lane          TEXT NOT NULL DEFAULT 'default',
    status        TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|error
    attempts      INTEGER NOT NULL DEFAULT 0,
    error         TEXT,
    created_at    TEXT NOT NULL,
    started_at    TEXT,
    finished_at   TEXT,
    duration_ms   INTEGER,
    trace_id      TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status ON jobs (status, id);
"""

# Which payload fields make a good human-readable label is APP knowledge —
# these are field names off one app's command models. Injected via
# `label_fields=`; this tuple is only the fallback for a host that doesn't
# care, and no behaviour depends on the specific names.
DEFAULT_LABEL_FIELDS: tuple[str, ...] = (
    "original_name",
    "url",
    "title",
    "body",
    "source_entry_id",
    "blob_hash",
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _label_for(command: Any, fields: tuple[str, ...]) -> str:
    payload = command.model_dump()
    for field in fields:
        value = payload.get(field)
        if value:
            return str(value)[:120]
    return ""


class DurableCommandQueue:
    """queue.Queue-compatible ``put`` plus claim/ack, all persisted to SQLite."""

    def __init__(
        self,
        registry: Mapping[str, Any],
        path: str | Path | None = None,
        lane_of: Any | None = None,
        label_fields: tuple[str, ...] | None = None,
    ):
        # registry: command class name -> pydantic class, for rehydration.
        # lane_of: callable(command) -> lane name; default everything to 'default'.
        # label_fields: payload fields to surface as a job label, in order.
        self.registry = registry
        self.lane_of = lane_of or (lambda _c: "default")
        self.label_fields = (
            tuple(label_fields) if label_fields is not None else DEFAULT_LABEL_FIELDS
        )
        self.path = Path(path or os.environ.get("DIZZY_COMMAND_QUEUE_PATH") or DEFAULT_QUEUE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # One shared connection; every access holds self._cond's lock.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(_SCHEMA)
        cols = [r[1] for r in self._db.execute("PRAGMA table_info('jobs')")]
        if "lane" not in cols:  # pre-lane database: migrate in place
            self._db.execute("ALTER TABLE jobs ADD COLUMN lane TEXT NOT NULL DEFAULT 'default'")
        if "trace_id" not in cols:  # pre-otel database: migrate in place
            self._db.execute("ALTER TABLE jobs ADD COLUMN trace_id TEXT")
        self._db.execute("CREATE INDEX IF NOT EXISTS jobs_lane ON jobs (lane, status, id)")
        self._db.commit()
        self._cond = threading.Condition()
        self._subscribers: list[queue.Queue] = []
        self.recovered = self._recover()
        self._prune()

    # ── Producer side (engine policies + HTTP endpoints call this) ───────────

    def put(self, command: Any, origin: str = "policy") -> int:
        row = {
            "command_type": type(command).__name__,
            "payload_json": command.model_dump_json(),
            "label": _label_for(command, self.label_fields),
            "origin": origin,
            "lane": self.lane_of(command),
            "created_at": _now(),
        }
        with self._cond:
            cur = self._db.execute(
                "INSERT INTO jobs (command_type, payload_json, label, origin,"
                " lane, status, created_at) VALUES (:command_type,"
                " :payload_json, :label, :origin, :lane, 'queued',"
                " :created_at)",
                row,
            )
            self._db.commit()
            job_id = int(cur.lastrowid or 0)
            self._notify(job_id)
            self._cond.notify_all()
        return job_id

    # ── Worker side ───────────────────────────────────────────────────────────

    def claim(self, timeout: float | None = None, lane: str = "default") -> tuple[int, Any] | None:
        """Block until a queued job exists ON THIS LANE; mark it running.

        Returns (job_id, command), or None on timeout. One worker per lane:
        the per-lane serialization is the chain's concurrency guard.
        """
        with self._cond:
            while True:
                row = self._db.execute(
                    "SELECT * FROM jobs WHERE status='queued' AND lane=? ORDER BY id LIMIT 1",
                    (lane,),
                ).fetchone()
                if row is not None:
                    break
                if not self._cond.wait(timeout=timeout):
                    return None
            self._db.execute(
                "UPDATE jobs SET status='running', started_at=?, attempts=attempts+1 WHERE id=?",
                (_now(), row["id"]),
            )
            self._db.commit()
            self._notify(row["id"])
        cls = self.registry.get(row["command_type"])
        if cls is None:
            self.mark_error(row["id"], f"unknown command type {row['command_type']!r}")
            return self.claim(timeout=timeout, lane=lane)
        return row["id"], cls.model_validate_json(row["payload_json"])

    def mark_running(self, job_id: int, trace_id: str | None = None) -> None:
        """External-executor status update: in mp mode this queue is a display
        LEDGER — a broker worker claimed the job, not a lane thread.
        trace_id: the worker's OTel trace, for queue-tab → trace links."""
        with self._cond:
            self._db.execute(
                "UPDATE jobs SET status='running', started_at=?,"
                " attempts=attempts+1, trace_id=COALESCE(?, trace_id)"
                " WHERE id=?",
                (_now(), trace_id, job_id),
            )
            self._db.commit()
            self._notify(job_id)

    def job_for_trace(self, trace_id: str) -> dict | None:
        """The ledger row a worker ran under this OTel trace (newest wins)."""
        with self._cond:
            row = self._db.execute(
                "SELECT * FROM jobs WHERE trace_id=? ORDER BY id DESC LIMIT 1", (trace_id,)
            ).fetchone()
        return dict(row) if row else None

    def mark_done(self, job_id: int) -> None:
        self._finish(job_id, "done", None)

    def mark_error(self, job_id: int, error: str) -> None:
        self._finish(job_id, "error", error[:4000])

    def _finish(self, job_id: int, status: str, error: str | None) -> None:
        with self._cond:
            row = self._db.execute("SELECT started_at FROM jobs WHERE id=?", (job_id,)).fetchone()
            duration_ms = None
            if row and row["started_at"]:
                started = datetime.fromisoformat(row["started_at"])
                duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
            self._db.execute(
                "UPDATE jobs SET status=?, error=?, finished_at=?, duration_ms=? WHERE id=?",
                (status, error, _now(), duration_ms, job_id),
            )
            self._db.commit()
            self._notify(job_id)
        if status == "done" and job_id % 50 == 0:
            self._prune()

    def retry(self, job_id: int) -> bool:
        """Requeue an errored job. Returns False if it isn't retryable."""
        with self._cond:
            cur = self._db.execute(
                "UPDATE jobs SET status='queued', error=NULL, started_at=NULL,"
                " finished_at=NULL, duration_ms=NULL"
                " WHERE id=? AND status='error'",
                (job_id,),
            )
            self._db.commit()
            if cur.rowcount == 0:
                return False
            self._notify(job_id)
            self._cond.notify_all()
        return True

    # ── Introspection (the /api/command-queue surface) ────────────────────────

    def counts(self) -> dict[str, int]:
        with self._cond:
            rows = self._db.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
        base = {"queued": 0, "running": 0, "done": 0, "error": 0}
        base.update({r["status"]: r["n"] for r in rows})
        return base

    def jobs(
        self, status: str | None = None, limit: int = 100, before_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Newest-first job rows, filterable by status, cursor-paginated by id."""
        clauses, params = [], []
        if status:
            clauses.append("status=?")
            params.append(status)
        if before_id is not None:
            clauses.append("id<?")
            params.append(before_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._cond:
            rows = self._db.execute(
                f"SELECT * FROM jobs {where} ORDER BY id DESC LIMIT ?", (*params, limit)
            ).fetchall()
        return [dict(r) for r in rows]

    def qsize(self) -> int:
        return self.counts()["queued"]

    # ── SSE fan-out ───────────────────────────────────────────────────────────

    def subscribe(self, maxsize: int = 500) -> queue.Queue[dict]:
        sub: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._cond:
            self._subscribers.append(sub)
        return sub

    def unsubscribe(self, sub: queue.Queue[dict]) -> None:
        with self._cond:
            if sub in self._subscribers:
                self._subscribers.remove(sub)

    def _notify(self, job_id: int) -> None:
        # Called with the lock held; pushes the fresh row to every subscriber.
        row = self._db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            return
        payload = dict(row)
        for sub in self._subscribers:
            try:
                sub.put_nowait(payload)
            except queue.Full:
                pass  # slow consumer: it re-syncs from the snapshot on reconnect

    # ── Housekeeping ──────────────────────────────────────────────────────────

    def _recover(self) -> int:
        """Crash recovery: anything mid-flight when the process died re-queues."""
        with self._cond:
            cur = self._db.execute("UPDATE jobs SET status='queued' WHERE status IN ('running')")
            self._db.commit()
            return cur.rowcount

    def _prune(self, keep: int = KEEP_FINISHED) -> None:
        with self._cond:
            self._db.execute(
                "DELETE FROM jobs WHERE status IN ('done','error') AND id NOT IN"
                " (SELECT id FROM jobs WHERE status IN ('done','error')"
                "  ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
            self._db.commit()

    def close(self) -> None:
        with self._cond:
            self._db.close()
