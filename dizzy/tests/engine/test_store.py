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
