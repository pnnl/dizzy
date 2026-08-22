"""Properties of the canonical serialization: determinism, subset enforcement."""

from __future__ import annotations

import json

import pytest
from dizzy.engine.dagstore import NotCanonicalizable, canonical_json
from hypothesis import given
from hypothesis import strategies as st

# The hashed subset: no floats, ints within ±(2^53−1), str keys only.
scalars = (
    st.none() | st.booleans() | st.integers(min_value=-(2**53) + 1, max_value=2**53 - 1) | st.text()
)
values = st.recursive(
    scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=20,
)


@given(values)
def test_roundtrip_stable(value):
    """canonical(parse(canonical(v))) == canonical(v) — the fixpoint property."""
    once = canonical_json(value)
    again = canonical_json(json.loads(once.decode("utf-8")))
    assert once == again


@given(st.dictionaries(st.text(max_size=8), scalars, min_size=2, max_size=6))
def test_insertion_order_irrelevant(d):
    reversed_d = dict(reversed(list(d.items())))
    assert canonical_json(d) == canonical_json(reversed_d)


def test_known_answer():
    assert canonical_json({"b": 1, "a": None}) == b'{"a":null,"b":1}'
    assert canonical_json(["x", True, -5]) == b'["x",true,-5]'


def test_absent_differs_from_null():
    assert canonical_json({}) != canonical_json({"x": None})


def test_floats_rejected():
    with pytest.raises(NotCanonicalizable):
        canonical_json({"score": 0.5})


def test_big_ints_rejected():
    with pytest.raises(NotCanonicalizable):
        canonical_json(2**53)
    canonical_json(2**53 - 1)  # boundary is allowed


def test_non_string_keys_rejected():
    with pytest.raises(NotCanonicalizable):
        canonical_json({1: "x"})


def test_utf16_key_order():
    # U+FF01 (BMP) sorts *after* U+1D306 under UTF-16 code units (surrogates
    # start at 0xD800 < 0xFF01), though code-point order says the opposite.
    out = canonical_json({"！": 1, "\U0001d306": 2}).decode("utf-8")
    assert out.index("\U0001d306") < out.index("！")
