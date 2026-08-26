"""The single-process shell — the durable queue's contract and the telemetry ring.

The queue's promise is that a command survives the SQLite round trip intact,
that work in flight when a process dies comes back, that lanes serialize
independently, and that priority reorders a claim without losing a row.
Its commands are duck-typed on the pydantic surface
(``model_dump``/``model_dump_json``/``model_validate_json``), so these tests
use a stand-in rather than generating a feature — what is under test is the
queue, not a schema.
"""

from __future__ import annotations

import json
import queue as queue_mod
import sqlite3

import pytest
from dizzy.engine.st import (
    DEFAULT_LABEL_FIELDS,
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    DurableCommandQueue,
    TelemetryBus,
)


class Ingest:
    """A stand-in for a generated command: the pydantic surface, by hand."""

    def __init__(self, blob_hash: str = "ab" * 32, original_name: str = "clip.mp4"):
        self.blob_hash = blob_hash
        self.original_name = original_name

    def model_dump(self) -> dict:
        return {"blob_hash": self.blob_hash, "original_name": self.original_name}

    def model_dump_json(self) -> str:
        return json.dumps(self.model_dump())

    @classmethod
    def model_validate_json(cls, payload: str) -> Ingest:
        return cls(**json.loads(payload))

    def __eq__(self, other) -> bool:
        return isinstance(other, Ingest) and self.model_dump() == other.model_dump()


REGISTRY = {"Ingest": Ingest}


@pytest.fixture
def make_queue(tmp_path):
    made: list[DurableCommandQueue] = []

    def build(name: str = "q.db", **kwargs) -> DurableCommandQueue:
        q = DurableCommandQueue(registry=REGISTRY, path=tmp_path / name, **kwargs)
        made.append(q)
        return q

    yield build
    for q in made:
        q.close()


# ── The round trip ──────────────────────────────────────────────────────────


def test_a_command_survives_the_round_trip(make_queue):
    q = make_queue()
    job_id = q.put(Ingest(), origin="upload")
    assert q.qsize() == 1

    claimed = q.claim(timeout=1)
    assert claimed is not None
    got_id, command = claimed
    assert got_id == job_id
    assert command == Ingest()

    q.mark_done(job_id)
    assert q.counts()["done"] == 1 and q.qsize() == 0


def test_claim_times_out_on_an_empty_lane(make_queue):
    assert make_queue().claim(timeout=0.05) is None


def test_an_unknown_command_type_errors_the_job_and_moves_on(make_queue, tmp_path):
    """A row whose class is gone (a renamed or removed command) must not wedge
    the lane — it fails and the next job proceeds."""
    stale = DurableCommandQueue(registry=REGISTRY, path=tmp_path / "q.db")
    stale.put(Ingest())
    stale.close()

    q = DurableCommandQueue(registry={}, path=tmp_path / "q.db")
    try:
        q.put(Ingest())  # still unknown to this registry
        assert q.claim(timeout=0.05) is None
        assert q.counts()["error"] == 2
        assert "unknown command type" in q.jobs(status="error")[0]["error"]
    finally:
        q.close()


# ── Durability ──────────────────────────────────────────────────────────────


def test_work_in_flight_when_the_process_dies_comes_back(make_queue, tmp_path):
    first = make_queue()
    first.put(Ingest())
    first.claim(timeout=1)  # now 'running'
    assert first.counts()["running"] == 1
    first.close()

    second = DurableCommandQueue(registry=REGISTRY, path=tmp_path / "q.db")
    try:
        assert second.recovered == 1
        assert second.counts()["queued"] == 1
    finally:
        second.close()


def test_an_errored_job_can_be_retried_and_nothing_else_can(make_queue):
    q = make_queue()
    job_id = q.put(Ingest())
    q.claim(timeout=1)
    q.mark_error(job_id, "boom")
    assert q.counts()["error"] == 1

    assert q.retry(job_id) is True
    assert q.counts()["queued"] == 1
    assert q.retry(job_id) is False  # no longer in 'error'
    assert q.retry(99_999) is False  # never existed


# ── Lanes ───────────────────────────────────────────────────────────────────


def test_lanes_are_claimed_independently(make_queue):
    q = make_queue(lane_of=lambda c: "chat" if c.original_name == "chat" else "default")
    q.put(Ingest(original_name="chat"))
    q.put(Ingest(original_name="clip.mp4"))

    chat = q.claim(timeout=1, lane="chat")
    default = q.claim(timeout=1, lane="default")
    assert chat is not None and chat[1].original_name == "chat"
    assert default is not None and default[1].original_name == "clip.mp4"
    assert q.claim(timeout=0.05, lane="chat") is None


# ── Priority ────────────────────────────────────────────────────────────────


def interactive_if_chat(command) -> int:
    return PRIORITY_INTERACTIVE if command.original_name == "chat" else PRIORITY_BATCH


def test_everything_is_batch_until_a_host_says_otherwise(make_queue):
    """The default has to be FIFO: a host that injects nothing must see the
    exact claim order it saw before priority existed."""
    q = make_queue()
    ids = [q.put(Ingest(original_name=f"{i}.mp4")) for i in range(4)]
    assert [row["priority"] for row in q.jobs()] == [PRIORITY_BATCH] * 4
    assert [q.claim(timeout=1)[0] for _ in ids] == ids


def test_an_interactive_command_is_claimed_ahead_of_queued_batch_work(make_queue):
    """Queue-position latency, stated as a test: arriving last is not the same
    as being served last."""
    q = make_queue(priority_of=interactive_if_chat)
    for i in range(20):
        q.put(Ingest(original_name=f"{i}.mp4"))
    urgent = q.put(Ingest(original_name="chat"))

    claimed = q.claim(timeout=1)
    assert claimed is not None and claimed[0] == urgent


def test_batch_work_is_deferred_not_skipped(make_queue):
    """The starvation bound that matters here: priority reorders a finite set,
    it does not drop from it. Every batch job still gets claimed, in its
    original relative order."""
    q = make_queue(priority_of=interactive_if_chat)
    batch = [q.put(Ingest(original_name=f"{i}.mp4")) for i in range(5)]
    urgent = q.put(Ingest(original_name="chat"))

    order = []
    while True:
        claimed = q.claim(timeout=0.05)
        if claimed is None:
            break
        order.append(claimed[0])
    assert order == [urgent, *batch]


def test_priority_orders_within_a_lane_not_across_lanes(make_queue):
    """Lanes stay independent: a high-priority job on another lane is invisible
    to this claim, because the lane is who serves it, not what it costs."""
    q = make_queue(
        lane_of=lambda c: "chat" if c.original_name == "chat" else "default",
        priority_of=interactive_if_chat,
    )
    first = q.put(Ingest(original_name="a.mp4"))
    q.put(Ingest(original_name="chat"))
    claimed = q.claim(timeout=1, lane="default")
    assert claimed is not None and claimed[0] == first


def test_subscribers_see_the_priority(make_queue):
    """The SSE ledger renders it, so it has to be on the record."""
    q = make_queue(priority_of=interactive_if_chat)
    sub = q.subscribe()
    q.put(Ingest(original_name="chat"))
    assert sub.get_nowait()["priority"] == PRIORITY_INTERACTIVE


def test_a_pre_priority_database_migrates_in_place(make_queue, tmp_path):
    """An existing queue file on disk must open and keep working — the whole
    reason the lane and trace_id columns were added the same way."""
    path = tmp_path / "old.db"
    legacy = sqlite3.connect(str(path))
    legacy.executescript(
        """
        CREATE TABLE jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            command_type  TEXT NOT NULL,
            payload_json  TEXT NOT NULL,
            label         TEXT NOT NULL DEFAULT '',
            origin        TEXT NOT NULL DEFAULT '',
            lane          TEXT NOT NULL DEFAULT 'default',
            status        TEXT NOT NULL DEFAULT 'queued',
            attempts      INTEGER NOT NULL DEFAULT 0,
            error         TEXT,
            created_at    TEXT NOT NULL,
            started_at    TEXT,
            finished_at   TEXT,
            duration_ms   INTEGER,
            trace_id      TEXT
        );
        CREATE INDEX jobs_lane ON jobs (lane, status, id);
        """
    )
    legacy.execute(
        "INSERT INTO jobs (command_type, payload_json, created_at)"
        " VALUES ('Ingest', ?, '2026-01-01T00:00:00+00:00')",
        (Ingest(original_name="legacy.mp4").model_dump_json(),),
    )
    legacy.commit()
    legacy.close()

    q = make_queue("old.db", priority_of=interactive_if_chat)
    assert q.jobs(limit=1)[0]["priority"] == PRIORITY_BATCH  # the pre-priority row
    urgent = q.put(Ingest(original_name="chat"))
    claimed = q.claim(timeout=1)
    assert claimed is not None and claimed[0] == urgent


# ── Labels are the host's vocabulary, not the shell's ──────────────────────


def test_label_fields_are_injected(make_queue):
    q = make_queue(label_fields=("original_name",))
    q.put(Ingest(original_name="holiday.mp4"))
    assert q.jobs(limit=1)[0]["label"] == "holiday.mp4"


def test_an_unmatched_label_field_is_simply_blank(make_queue):
    q = make_queue(label_fields=("no_such_field",))
    q.put(Ingest())
    assert q.jobs(limit=1)[0]["label"] == ""


def test_the_default_label_fields_are_a_fallback_not_knowledge():
    """They exist so a host that doesn't care gets something readable; no
    behaviour may depend on the particular names."""
    assert isinstance(DEFAULT_LABEL_FIELDS, tuple)
    assert all(isinstance(f, str) for f in DEFAULT_LABEL_FIELDS)


# ── Fan-out ─────────────────────────────────────────────────────────────────


def test_subscribers_see_every_transition(make_queue):
    q = make_queue()
    sub = q.subscribe()
    job_id = q.put(Ingest())
    q.claim(timeout=1)
    q.mark_done(job_id)

    seen = []
    while True:
        try:
            seen.append(sub.get_nowait())
        except queue_mod.Empty:
            break
    assert [row["status"] for row in seen] == ["queued", "running", "done"]

    q.unsubscribe(sub)
    q.put(Ingest())
    with pytest.raises(queue_mod.Empty):
        sub.get_nowait()


def test_a_slow_subscriber_is_dropped_not_blocking(make_queue):
    """Observability is never load-bearing: a full subscriber queue must not
    stall the producer."""
    q = make_queue()
    q.subscribe(maxsize=1)
    for _ in range(5):
        q.put(Ingest())
    assert q.qsize() == 5


# ── The telemetry ring ──────────────────────────────────────────────────────


def test_the_ring_is_ordered_cursored_and_filterable():
    bus = TelemetryBus(maxlen=10)
    bus.emit({"kind": "procedure", "name": "a"})
    bus.emit({"kind": "policy", "name": "b"})

    records = bus.since()
    assert [r["seq"] for r in records] == [1, 2]
    assert all("ts" in r for r in records)
    assert [r["name"] for r in bus.since(after=1)] == ["b"]
    assert [r["name"] for r in bus.since(kind="policy")] == ["b"]
    assert bus.since(after=2) == []


def test_the_ring_is_bounded_and_drops_the_oldest():
    """Deliberately ephemeral — the event store is the truth."""
    bus = TelemetryBus(maxlen=3)
    for i in range(5):
        bus.emit({"kind": "event", "name": str(i)})
    assert [r["name"] for r in bus.since()] == ["2", "3", "4"]
