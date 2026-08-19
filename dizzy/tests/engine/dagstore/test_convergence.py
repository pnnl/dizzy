"""The flagship property: convergence.

Simulated fleets of nodes append and sync in random interleavings; after full
anti-entropy, every node must replay the identical byte sequence. This is the
invariant the whole engine design leans on: *any two stores holding the same
heads rebuild byte-identical models.*
"""

from __future__ import annotations

from dizzy.engine.dagstore import DagStore, canonical_json, sync
from hypothesis import given, settings
from hypothesis import strategies as st

payloads = st.dictionaries(
    st.text(min_size=1, max_size=6),
    st.none() | st.booleans() | st.integers(-1000, 1000) | st.text(max_size=8),
    max_size=3,
)


def fingerprint(store: DagStore) -> bytes:
    """The canonical replay, as bytes — what 'same results' means."""
    return b"\n".join(
        canonical_json(
            {"id": e.id, "type": e.type, "parents": list(e.parents), "payload": e.payload}
        )
        for e in store.iterate()
    )


def full_anti_entropy(stores: list[DagStore]) -> None:
    """Sync all pairs until a fixpoint (bounded — gossip floods in ≤n rounds)."""
    for _ in range(len(stores) + 1):
        moved = 0
        for dst in stores:
            for src in stores:
                if dst is not src:
                    moved += sync(dst, src)
        if moved == 0:
            return
    raise AssertionError("anti-entropy failed to reach fixpoint")


@settings(max_examples=200, deadline=None)
@given(data=st.data())
def test_fleet_converges(data):
    """Random appends + random partial syncs across 2–4 nodes → after full
    anti-entropy, identical heads and byte-identical canonical replay."""
    n_nodes = data.draw(st.integers(2, 4), label="n_nodes")
    stores = [DagStore() for _ in range(n_nodes)]

    n_ops = data.draw(st.integers(1, 25), label="n_ops")
    for _ in range(n_ops):
        if data.draw(st.booleans(), label="is_append"):
            node = data.draw(st.integers(0, n_nodes - 1), label="appender")
            stores[node].append("evt", data.draw(payloads, label="payload"))
        else:
            i = data.draw(st.integers(0, n_nodes - 1), label="dst")
            j = data.draw(st.integers(0, n_nodes - 1), label="src")
            if i != j:
                sync(stores[i], stores[j])

    full_anti_entropy(stores)

    heads = {s.heads() for s in stores}
    assert len(heads) == 1, "converged nodes must agree on the frontier"
    prints = {fingerprint(s) for s in stores}
    assert len(prints) == 1, "converged nodes must replay byte-identically"


@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_replay_independent_of_arrival_order(data):
    """Build a DAG, then re-deliver its events to fresh stores in different
    random (ancestry-respecting) orders — replay must not notice."""
    origin = DagStore()
    n = data.draw(st.integers(1, 15), label="n_events")
    for k in range(n):
        origin.append("evt", data.draw(payloads, label=f"payload_{k}"))
        # occasionally fork: merge in a burst from a scratch peer
        if data.draw(st.booleans(), label=f"fork_{k}"):
            peer = DagStore()
            peer.append("evt", data.draw(payloads, label=f"fork_payload_{k}"))
            sync(origin, peer)

    events = list(origin.iterate())
    replica = DagStore()
    pending = list(events)
    while pending:
        deliverable = [e for e in pending if all(p in replica for p in e.parents)]
        pick = data.draw(st.sampled_from(deliverable), label="delivery")
        replica.add(pick)
        pending.remove(pick)

    assert fingerprint(replica) == fingerprint(origin)
    assert replica.heads() == origin.heads()


@settings(max_examples=100, deadline=None)
@given(data=st.data())
def test_same_heads_implies_same_replay(data):
    """The checkpoint claim: heads fully determine the replay."""
    a, b = DagStore(), DagStore()
    for _ in range(data.draw(st.integers(1, 10), label="n")):
        a.append("evt", data.draw(payloads, label="p"))
        sync(b, a)
        if data.draw(st.booleans(), label="b_appends"):
            b.append("evt", data.draw(payloads, label="q"))
            sync(a, b)
    if a.heads() == b.heads():
        assert fingerprint(a) == fingerprint(b)
    else:  # not yet converged — one more exchange must get there
        sync(a, b), sync(b, a)
        assert a.heads() == b.heads()
        assert fingerprint(a) == fingerprint(b)
