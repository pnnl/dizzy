"""The event store: append is the truth, and the feat supplies the type map.

These tests use real pydantic models rather than the ``def_package`` stand-in,
because what is under test is the round trip through ``model_dump`` and
``cls(**payload)`` — the one place the runtime genuinely needs pydantic.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from dizzy.engine.store import Envelope, EventStore, reconstruct_event
from pydantic import BaseModel


class RecipeDefined(BaseModel):
    recipe_id: str
    servings: int = 1


class BatchOpened(BaseModel):
    batch_id: str
    yield_ratio: float = 0.0


EVENT_CLASSES = {"recipe_defined": RecipeDefined, "batch_opened": BatchOpened}


@pytest.fixture
def store(tmp_path):
    return EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)


def test_append_returns_an_envelope_naming_the_event_as_the_feat_does(store):
    envelope = store.append(RecipeDefined(recipe_id="r1"))
    assert envelope.type == "recipe_defined"
    assert envelope.payload == {"recipe_id": "r1", "servings": 1}
    assert envelope.ingested_at.tzinfo is not None
    assert len(envelope.id) == 64  # sha-256 hex


def test_appends_chain_so_order_is_parent_pointers_not_a_counter(store):
    first = store.append(RecipeDefined(recipe_id="r1"))
    second = store.append(RecipeDefined(recipe_id="r2"))
    assert second.parents == (first.id,)
    assert store.heads() == (second.id,)
    assert len(store) == 2


def test_iterate_yields_canonical_order_with_a_derived_seq(store):
    store.append(RecipeDefined(recipe_id="r1"))
    store.append(RecipeDefined(recipe_id="r2"))
    envelopes = list(store.iterate())
    assert [e.payload["recipe_id"] for e in envelopes] == ["r1", "r2"]
    assert [e.seq for e in envelopes] == [0, 1]


def test_ingested_at_survives_the_round_trip(store):
    stamped = datetime(2026, 8, 18, 12, 0, tzinfo=UTC)
    store.append(RecipeDefined(recipe_id="r1"), ingested_at=stamped)
    (envelope,) = list(store.iterate())
    assert envelope.ingested_at == stamped


def test_a_supplied_ingested_at_is_what_replication_reuses(store):
    """Replaying an already-stamped fact must not restamp it — the append
    timestamp is part of the fact, not of this run."""
    old = datetime(2020, 1, 1, tzinfo=UTC)
    envelope = store.append(RecipeDefined(recipe_id="r1"), ingested_at=old)
    assert envelope.ingested_at == old


def test_floats_survive_the_canonical_form_exactly(store):
    """The hashed subset forbids floats, so they travel as reprs and come back
    through pydantic's lax coercion."""
    store.append(BatchOpened(batch_id="b1", yield_ratio=0.30000000000000004))
    (envelope,) = list(store.iterate())
    assert envelope.payload["yield_ratio"] == "0.30000000000000004"
    event = store.reconstruct_event(envelope)
    assert event.yield_ratio == 0.30000000000000004


def test_reconstruct_returns_the_event_instance(store):
    store.append(RecipeDefined(recipe_id="r1", servings=4))
    (envelope,) = list(store.iterate())
    event = store.reconstruct_event(envelope)
    assert isinstance(event, RecipeDefined)
    assert event == RecipeDefined(recipe_id="r1", servings=4)


def test_an_event_the_feat_does_not_declare_is_named_in_the_error():
    envelope = Envelope(id="x", type="something_else", ingested_at=datetime.now(UTC), payload={})
    with pytest.raises(KeyError, match="something_else"):
        reconstruct_event(envelope, EVENT_CLASSES)


def test_identical_content_hashes_identically(tmp_path):
    """Two stores fed the same fact agree on its identity — the property the
    whole content-addressed design rests on."""
    stamped = datetime(2026, 8, 18, tzinfo=UTC)
    a = EventStore(path=tmp_path / "a.db", event_classes=EVENT_CLASSES)
    b = EventStore(path=tmp_path / "b.db", event_classes=EVENT_CLASSES)
    one = a.append(RecipeDefined(recipe_id="r1"), ingested_at=stamped)
    two = b.append(RecipeDefined(recipe_id="r1"), ingested_at=stamped)
    assert one.id == two.id


def test_the_event_map_comes_from_the_feat_when_not_supplied(
    tmp_path, write_feat, def_package, monkeypatch
):
    """The store must not scan a generated module; it reads the feat."""
    pkg = def_package(events=["RecipeDefined"])
    feat = write_feat("name: kitchen\nevents:\n  recipe_defined: a recipe was defined\n")
    from dizzy.engine.registry import FeatGraph

    graph = FeatGraph.load(feat, def_package=pkg)
    store = EventStore(path=tmp_path / "events.db", graph=graph)
    assert set(store.event_classes) == {"recipe_defined"}


def test_the_event_map_is_resolved_lazily(tmp_path):
    """A worker that only appends must never import the generated package."""
    store = EventStore(path=tmp_path / "events.db", graph=None)
    store.append(RecipeDefined(recipe_id="r1"))  # no feat, no generated import
    assert len(store) == 1


def test_the_store_path_can_come_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DIZZY_STORE_PATH", str(tmp_path / "nested" / "events.db"))
    store = EventStore(event_classes=EVENT_CLASSES)
    assert store.path == tmp_path / "nested" / "events.db"
    assert store.path.parent.is_dir()


# ── canonical order is not a stable coordinate system ────────────────────────


def _peer_fork(parent_id, recipe_id="p1", below=None):
    """Mint a fact on a *peer*, forking off ``parent_id``.

    Built with the dagstore's own primitives so the hash is real: this is
    exactly the shape `replicate.pull` delivers.

    ``below`` searches for a payload whose content hash sorts below that id.
    Canonical order tiebreaks concurrent siblings by hash, so without pinning
    the tiebreak a fork test would pass or fail on the luck of a sha-256 —
    the search makes "the peer wins the tiebreak" a stated precondition
    instead of a coin flip.
    """
    from dizzy.engine.dagstore.events import make_event

    stamped = datetime(2026, 1, 1, tzinfo=UTC).isoformat()
    for n in range(10_000):
        name = recipe_id if below is None else f"{recipe_id}-{n}"
        event = make_event(
            "recipe_defined",
            (parent_id,),
            {"ingested_at": stamped, "event": {"recipe_id": name, "servings": 1}},
        )
        if below is None or event.id < below:
            return event
    raise AssertionError("no payload hashed below the sibling — vanishingly unlikely")


def test_add_replicated_splices_into_the_middle_of_canonical_order(store):
    """THE finding a cursor design turns on: a replicated fact does NOT land at
    the end of `iterate()`. It lands wherever its parent puts it, renumbering
    every event after the fork — so a stored canonical index is not a position,
    it is a guess that goes stale the first time a peer's fact arrives.
    """
    locals_ = [store.append(RecipeDefined(recipe_id=f"r{i}")) for i in range(5)]
    before = [e.id for e in store.iterate()]

    # Fork off the SECOND local event, the way a peer that had only seen r0/r1
    # would have. Its sibling is r2, and the id tiebreak decides which of the
    # two goes first — but either way it precedes everything downstream of r2.
    peer = _peer_fork(locals_[1].id, below=locals_[2].id)
    store.add_replicated(peer)

    after = [e.id for e in store.iterate()]
    assert after.index(peer.id) < len(after) - 1, "replicated fact did not land at the tail"
    assert after[:2] == before[:2], "the prefix before the fork must not move"
    moved = [eid for eid in before[2:] if before.index(eid) != after.index(eid)]
    assert moved == before[2:], "every event after the fork should have been renumbered"


def test_a_saved_canonical_seq_points_at_a_different_event_after_replication(store):
    """The same finding stated as the consequence: seq is not a cursor."""
    store.append(RecipeDefined(recipe_id="r1"))
    store.append(RecipeDefined(recipe_id="r2"))
    first, second = list(store.iterate())
    assert second.seq == 1

    store.add_replicated(_peer_fork(first.id, below=second.id))
    at_seq_1 = list(store.iterate())[1]
    assert at_seq_1.id != second.id


# ── iterate_from: the resumable cursor ───────────────────────────────────────


def test_iterate_from_zero_is_the_whole_stream(store):
    for i in range(3):
        store.append(RecipeDefined(recipe_id=f"r{i}"))
    assert [e.payload["recipe_id"] for e in store.iterate_from(0)] == ["r0", "r1", "r2"]


def test_iterate_from_an_empty_store_yields_nothing(store):
    assert list(store.iterate_from(0)) == []
    assert list(store.iterate_from(999)) == []


def test_iterate_from_past_the_end_yields_nothing(store):
    store.append(RecipeDefined(recipe_id="r1"))
    (only,) = list(store.iterate_from(0))
    assert list(store.iterate_from(only.position)) == []


def test_a_worker_resumes_with_no_gap_and_no_repeat(store):
    """The property the whole thing exists for."""
    for i in range(10):
        store.append(RecipeDefined(recipe_id=f"r{i}"))

    seen = []
    for envelope in store.iterate_from(0):
        seen.append(envelope.payload["recipe_id"])
        if len(seen) == 4:
            break
    checkpoint = envelope.position

    tail = list(store.iterate_from(checkpoint))
    assert [e.payload["recipe_id"] for e in tail] == [f"r{i}" for i in range(4, 10)]

    checkpoint = tail[-1].position
    assert list(store.iterate_from(checkpoint)) == []

    for i in range(10, 13):
        store.append(RecipeDefined(recipe_id=f"r{i}"))
    more = list(store.iterate_from(checkpoint))
    assert [e.payload["recipe_id"] for e in more] == ["r10", "r11", "r12"]
    assert seen + [e.payload["recipe_id"] for e in tail] + [
        e.payload["recipe_id"] for e in more
    ] == [f"r{i}" for i in range(13)]


def test_positions_are_monotonic_and_survive_a_mid_stream_replication(store):
    """A replicated fact appends at the cursor's tail even though canonically
    it belongs in the middle — so a saved position stays valid, and the worker
    is handed the peer's fact rather than skipping over it."""
    locals_ = [store.append(RecipeDefined(recipe_id=f"r{i}")) for i in range(4)]
    checkpoint = list(store.iterate_from(0))[1].position

    peer = _peer_fork(locals_[0].id, below=locals_[1].id)
    store.add_replicated(peer)

    tail = list(store.iterate_from(checkpoint))
    assert [e.id for e in tail] == [locals_[2].id, locals_[3].id, peer.id]
    assert [e.position for e in tail] == sorted(e.position for e in tail)


def test_appends_position_names_its_own_row_under_a_second_writer(tmp_path):
    """The position `append` hands back must be the row it wrote, not a count
    of the file. Two stores on one path is the multi-process shape a consumer
    group exists for, and a position naming a PEER's row would make a hybrid
    worker skip an entry it never folded — silent at-most-once."""
    a = EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)
    b = EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)
    written = [
        a.append(RecipeDefined(recipe_id="a1")),
        b.append(RecipeDefined(recipe_id="b1")),
        a.append(RecipeDefined(recipe_id="a2")),
    ]
    by_position = {e.position: e.payload["recipe_id"] for e in written}
    assert len(by_position) == 3
    for envelope in a.iterate_from(0):
        assert by_position[envelope.position] == envelope.payload["recipe_id"]


def test_the_cursor_order_still_puts_parents_before_children(store):
    """Insertion order is not canonical order, but it is always *a* causal
    order — that is what makes it safe to fold."""
    locals_ = [store.append(RecipeDefined(recipe_id=f"r{i}")) for i in range(3)]
    store.add_replicated(_peer_fork(locals_[0].id, below=locals_[1].id))
    emitted: set[str] = set()
    for envelope in store.iterate_from(0):
        assert all(p in emitted for p in envelope.parents)
        emitted.add(envelope.id)


def test_iterate_from_crosses_its_batch_boundary(store):
    """Batching is an implementation detail; the stream must not stutter at it."""
    import inspect

    from dizzy.engine.dagstore import DagStore

    n = int(inspect.signature(DagStore.read_after).parameters["limit"].default) + 5
    for i in range(n):
        store.append(RecipeDefined(recipe_id=f"r{i}"))
    got = list(store.iterate_from(0))
    assert [e.payload["recipe_id"] for e in got] == [f"r{i}" for i in range(n)]
    assert len({e.position for e in got}) == n
