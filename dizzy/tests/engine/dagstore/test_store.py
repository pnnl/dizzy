"""Unit-level properties of a single DagStore: append, add, heads, tampering."""

from __future__ import annotations

import pytest
from dizzy.engine.dagstore import (
    DagStore,
    Event,
    MissingParents,
    NotCanonicalizable,
    TamperedEvent,
    verify,
)


def test_append_chains_on_heads():
    s = DagStore()
    a = s.append("t", {"n": 1})
    assert a.parents == ()
    b = s.append("t", {"n": 2})
    assert b.parents == (a.id,)
    assert s.heads() == (b.id,)
    assert len(s) == 2


def test_same_appends_same_ids():
    """Hashing is deterministic: two fresh stores fed identically agree on ids."""
    s1, s2 = DagStore(), DagStore()
    for s in (s1, s2):
        s.append("t", {"n": 1})
        s.append("u", {"n": 2})
    assert s1.ids() == s2.ids()
    assert s1.heads() == s2.heads()


def test_get_roundtrip_verifies():
    s = DagStore()
    a = s.append("t", {"k": "v", "list": [1, None, "x"]})
    got = s.get(a.id)
    assert got == a
    assert verify(got)


def test_add_duplicate_is_noop():
    s = DagStore()
    a = s.append("t", {"n": 1})
    assert s.add(a) is False
    assert len(s) == 1


def test_add_rejects_tampered():
    s, other = DagStore(), DagStore()
    a = other.append("t", {"n": 1})
    forged = Event(id=a.id, type=a.type, parents=a.parents, payload={"n": 2})
    with pytest.raises(TamperedEvent):
        s.add(forged)


def test_add_requires_parents():
    src, dst = DagStore(), DagStore()
    src.append("t", {"n": 1})
    b = src.append("t", {"n": 2})
    with pytest.raises(MissingParents):
        dst.add(b)
    assert len(dst) == 0  # rejected atomically


def test_append_rejects_uncanonicalizable_payload():
    s = DagStore()
    with pytest.raises(NotCanonicalizable):
        s.append("t", {"score": 0.5})
    assert len(s) == 0


def test_merge_produces_two_heads_then_one():
    """Two independent chains merge; the next append closes the frontier."""
    from dizzy.engine.dagstore import sync

    s1, s2 = DagStore(), DagStore()
    a = s1.append("t", {"who": "s1"})
    b = s2.append("t", {"who": "s2"})
    sync(s1, s2)
    assert s1.heads() == tuple(sorted([a.id, b.id]))
    c = s1.append("t", {"who": "merge"})
    assert c.parents == tuple(sorted([a.id, b.id]))
    assert s1.heads() == (c.id,)


def test_iterate_parents_before_children():
    s = DagStore()
    for n in range(10):
        s.append("t", {"n": n})
    seen: set[str] = set()
    for event in s.iterate():
        assert all(p in seen for p in event.parents)
        seen.add(event.id)
    assert len(seen) == 10
