"""Rebuild: the read model is a cache, the stream is the truth.

``session`` and ``metadatas`` are duck-typed on purpose — the rebuild algorithm
imports no ORM — so these tests use stand-ins rather than SQLAlchemy, which is
itself the property under test.
"""

from __future__ import annotations

import io

import pytest
from dizzy.engine.rebuild import rebuild
from dizzy.engine.store import EventStore
from pydantic import BaseModel


class RecipeDefined(BaseModel):
    recipe_id: str


class BatchOpened(BaseModel):
    batch_id: str


EVENT_CLASSES = {"recipe_defined": RecipeDefined, "batch_opened": BatchOpened}


class Metadata:
    def __init__(self):
        self.calls = []

    def drop_all(self, bind):
        self.calls.append("drop")

    def create_all(self, bind):
        self.calls.append("create")


class Session:
    def __init__(self):
        self.commits = 0

    def get_bind(self):
        return "bind"

    def commit(self):
        self.commits += 1


@pytest.fixture
def store(tmp_path):
    store = EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)
    store.append(RecipeDefined(recipe_id="r1"))
    store.append(BatchOpened(batch_id="b1"))
    return store


def test_every_event_is_refolded_through_its_projections(store):
    folded = []
    runners = {
        RecipeDefined: [("recipe_book", lambda e, at: folded.append(e.recipe_id))],
        BatchOpened: [("batches", lambda e, at: folded.append(e.batch_id))],
    }
    session = Session()
    count = rebuild(store, session, runners, [Metadata()])
    assert count == 2
    assert folded == ["r1", "b1"]
    assert session.commits == 1


def test_tables_are_dropped_before_they_are_recreated(store):
    md = Metadata()
    rebuild(store, Session(), {}, [md])
    assert md.calls == ["drop", "create"]


def test_all_metadatas_drop_before_any_creates(store):
    """Two passes, not one per metadata: a table cannot be recreated while a
    later metadata still has to drop something that references it."""
    order = []

    class Tracked(Metadata):
        def __init__(self, tag):
            super().__init__()
            self.tag = tag

        def drop_all(self, bind):
            order.append(f"drop-{self.tag}")

        def create_all(self, bind):
            order.append(f"create-{self.tag}")

    rebuild(store, Session(), {}, [Tracked("a"), Tracked("b")])
    assert order == ["drop-a", "drop-b", "create-a", "create-b"]


def test_the_original_ingested_at_is_reused_not_restamped(store):
    """Both time axes survive a rebuild; only the fold wall-clock changes."""
    seen = []
    runners = {RecipeDefined: [("recipe_book", lambda e, at: seen.append(at))]}
    rebuild(store, Session(), runners, [Metadata()])
    originals = [e.ingested_at for e in store.iterate() if e.type == "recipe_defined"]
    assert seen == originals


def test_a_retired_event_type_is_skipped_not_fatal(tmp_path):
    """The stream is append-only, so it outlives the features that read it."""
    store = EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)
    store.append(RecipeDefined(recipe_id="r1"))
    store.append(BatchOpened(batch_id="b1"))
    # BatchOpened is no longer declared by the feat
    store._event_classes = {"recipe_defined": RecipeDefined}
    report = io.StringIO()
    folded = []
    runners = {RecipeDefined: [("recipe_book", lambda e, at: folded.append(e))]}
    count = rebuild(store, Session(), runners, [Metadata()], report=report)
    assert count == 1
    assert len(folded) == 1
    assert "batch_opened" in report.getvalue()


def test_an_event_with_no_projection_still_counts_as_folded(store):
    """Folding zero projections is a valid outcome — not every event feeds a
    read model — and must not look like a skipped fact."""
    assert rebuild(store, Session(), {}, [Metadata()]) == 2
