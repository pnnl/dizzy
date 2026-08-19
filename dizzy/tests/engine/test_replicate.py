"""Replication: a peer's facts arrive with their identity and their time intact.

The convergence property itself is pinned in ``dagstore/test_convergence.py``;
what is under test here is the DIZZY layer on top — that a pulled fact keeps
the id the peer minted and the ``ingested_at`` it was first stamped with, and
that fold-on-replicate runs the SAME projections a local emit would.
"""

from __future__ import annotations

import pytest
from dizzy.engine.replicate import file_transport, fold_envelopes, http_transport, pull
from dizzy.engine.store import EventStore
from pydantic import BaseModel


class RecipeDefined(BaseModel):
    recipe_id: str


class BatchOpened(BaseModel):
    batch_id: str


EVENT_CLASSES = {"recipe_defined": RecipeDefined, "batch_opened": BatchOpened}


class Session:
    def __init__(self):
        self.commits = 0

    def commit(self):
        self.commits += 1


@pytest.fixture
def peer(tmp_path):
    store = EventStore(path=tmp_path / "peer.db", event_classes=EVENT_CLASSES)
    store.append(RecipeDefined(recipe_id="r1"))
    store.append(BatchOpened(batch_id="b1"))
    return store


@pytest.fixture
def local(tmp_path):
    return EventStore(path=tmp_path / "local.db", event_classes=EVENT_CLASSES)


def test_pull_fetches_everything_the_peer_has_that_we_lack(local, peer, tmp_path):
    added = pull(local, *file_transport(str(tmp_path / "peer.db")))
    assert [e.type for e in added] == ["recipe_defined", "batch_opened"]
    assert len(local) == 2


def test_a_pulled_fact_keeps_the_id_the_peer_minted(local, peer, tmp_path):
    """Content addressing means replication is idempotent by construction."""
    peer_ids = [e.id for e in peer.iterate()]
    pull(local, *file_transport(str(tmp_path / "peer.db")))
    assert [e.id for e in local.iterate()] == peer_ids


def test_a_pulled_fact_keeps_its_original_ingested_at(local, peer, tmp_path):
    """Both time axes survive the hop: the append timestamp is part of the
    fact, not of the pull."""
    stamped = {e.id: e.ingested_at for e in peer.iterate()}
    pull(local, *file_transport(str(tmp_path / "peer.db")))
    assert {e.id: e.ingested_at for e in local.iterate()} == stamped


def test_pulling_twice_transfers_nothing_the_second_time(local, peer, tmp_path):
    """Stop-at-known: the second pull walks back into events we already hold."""
    transport = file_transport(str(tmp_path / "peer.db"))
    assert len(pull(local, *transport)) == 2
    assert pull(local, *file_transport(str(tmp_path / "peer.db"))) == []
    assert len(local) == 2


def test_parents_arrive_before_children(local, peer, tmp_path):
    """The DAG refuses an event whose ancestry is missing, so delivery order
    is a correctness requirement, not a preference."""
    added = pull(local, *file_transport(str(tmp_path / "peer.db")))
    seen: set[str] = set()
    for envelope in added:
        assert all(p in seen for p in envelope.parents)
        seen.add(envelope.id)


def test_fold_on_replicate_runs_the_same_projections_a_local_emit_would(local, peer, tmp_path):
    added = pull(local, *file_transport(str(tmp_path / "peer.db")))
    folded = []
    runners = {
        RecipeDefined: [("recipe_book", lambda e, at: folded.append(("recipe", at)))],
        BatchOpened: [("batches", lambda e, at: folded.append(("batch", at)))],
    }
    session = Session()
    count = fold_envelopes(added, session, runners, local.reconstruct_event)
    assert count == 2
    assert [name for name, _ in folded] == ["recipe", "batch"]
    assert [at for _, at in folded] == [e.ingested_at for e in added]


def test_the_fold_commits_per_event_so_a_crash_strands_nothing(local, peer, tmp_path):
    """The events are already in the local DAG, so the next pull will not
    re-fetch them — an un-committed fold would be lost for good."""
    added = pull(local, *file_transport(str(tmp_path / "peer.db")))
    session = Session()
    fold_envelopes(added, session, {}, local.reconstruct_event)
    assert session.commits == 2


def test_a_retired_type_replicates_as_a_fact_but_folds_nothing(local, peer, tmp_path):
    added = pull(local, *file_transport(str(tmp_path / "peer.db")))
    local._event_classes = {"recipe_defined": RecipeDefined}  # batch_opened retired
    session = Session()
    assert fold_envelopes(added, session, {}, local.reconstruct_event) == 1
    assert len(local) == 2  # the fact is still here


def test_the_http_transport_speaks_the_replication_surface(local, peer):
    """A stub client stands in for httpx — the transport is a pair of closures,
    so nothing here needs a network or a server."""

    class Response:
        def __init__(self, data):
            self._data = data

        def json(self):
            return self._data

    class Client:
        def get(self, url):
            if url.endswith("/replicate/heads"):
                return Response({"heads": list(peer.heads())})
            event_id = url.rsplit("/", 1)[-1]
            ev = peer.raw_event(event_id)
            return Response(
                {"id": ev.id, "type": ev.type, "parents": list(ev.parents), "payload": ev.payload}
            )

    added = pull(local, *http_transport("http://peer", client=Client()))
    assert [e.type for e in added] == ["recipe_defined", "batch_opened"]
    assert [e.id for e in local.iterate()] == [e.id for e in peer.iterate()]
