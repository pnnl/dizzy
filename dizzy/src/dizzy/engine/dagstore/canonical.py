"""Canonical JSON for hashing — RFC 8785 (JCS) on a deliberately narrowed subset.

This is the single serialization the content hash is computed over. Transport
codecs may re-encode events however they like; this form is the identity-bearing
one, so it must be reproducible byte-for-byte in any language. The house rules
that narrow plain JSON down to something a hash can tolerate:

- **No floats.** Amounts, durations, scores travel as ints or decimal strings.
  (JCS does define float serialization, but requiring every future Rust/TS
  worker to reproduce ECMAScript float formatting is a portability trap.)
- **Ints stay inside ±(2^53 − 1).** Beyond that, ECMAScript-style serializers
  switch to exponent notation while Python would print full decimal — so the
  range where the two agree is the only range allowed.
- **Absent ≠ null.** Both are representable and hash differently; emitters
  must omit absent fields. This module can't enforce the convention, but it
  round-trips both faithfully so violations are at least visible.
- **Object keys sort by UTF-16 code units** (per JCS), not Python's code-point
  order. The two differ only for keys containing astral-plane characters.
"""

from __future__ import annotations

import json
from typing import Any

__all__ = ["canonical_json", "NotCanonicalizable"]

# ECMAScript's exactly-representable integer range; the only range where
# JCS number formatting and plain decimal printing agree.
_MAX_SAFE_INT = 2**53 - 1


class NotCanonicalizable(ValueError):
    """The value falls outside the hashed subset (float, big int, non-str key…)."""


def _utf16_key(key: str) -> bytes:
    return key.encode("utf-16-be")


def _serialize(value: Any, out: list[str]) -> None:
    # bool must be tested before int (bool is an int subclass)
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, int):
        if abs(value) > _MAX_SAFE_INT:
            raise NotCanonicalizable(f"int {value} exceeds ±(2^53−1); use a decimal string")
        out.append(str(value))
    elif isinstance(value, float):
        raise NotCanonicalizable(
            f"floats are not canonicalizable ({value!r}); use int or decimal string"
        )
    elif isinstance(value, str):
        # Python's escaping with ensure_ascii=False matches JCS: short escapes
        # for \b \t \n \f \r \" \\, \u00xx for other control chars, everything
        # else literal UTF-8.
        out.append(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _serialize(item, out)
        out.append("]")
    elif isinstance(value, dict):
        out.append("{")
        for key in value:
            if not isinstance(key, str):
                raise NotCanonicalizable(f"object key {key!r} is not a string")
        for i, key in enumerate(sorted(value, key=_utf16_key)):
            if i:
                out.append(",")
            out.append(json.dumps(key, ensure_ascii=False))
            out.append(":")
            _serialize(value[key], out)
        out.append("}")
    else:
        raise NotCanonicalizable(f"{type(value).__name__} is outside the canonical subset")


def canonical_json(value: Any) -> bytes:
    """Serialize *value* to canonical JSON bytes (UTF-8).

    Deterministic: equal values (regardless of dict insertion order) always
    produce identical bytes. Raises NotCanonicalizable for anything outside
    the hashed subset.
    """
    out: list[str] = []
    _serialize(value, out)
    return "".join(out).encode("utf-8")
