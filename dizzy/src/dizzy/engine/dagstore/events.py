"""The event envelope and its content hash.

An event's stream id is SHA-256 over the canonical JSON of
``{"v": 1, "type": ..., "parents": [...sorted...], "payload": ...}``.
The ``v`` field versions the hash format itself; parents are sorted so
sibling order can't mint two ids for the same event. The id is therefore
self-verifying: anyone holding the event can recompute it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from dizzy.engine.dagstore.canonical import canonical_json

__all__ = ["Event", "compute_id", "make_event", "verify", "TamperedEvent"]

HASH_FORMAT_VERSION = 1


class TamperedEvent(ValueError):
    """An event's id does not match its content."""


@dataclass(frozen=True)
class Event:
    id: str
    type: str
    parents: tuple[str, ...]  # always sorted
    payload: dict[str, Any] = field(hash=False)


def compute_id(type: str, parents: tuple[str, ...] | list[str], payload: dict[str, Any]) -> str:
    body = {
        "v": HASH_FORMAT_VERSION,
        "type": type,
        "parents": sorted(parents),
        "payload": payload,
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


def make_event(type: str, parents: tuple[str, ...] | list[str], payload: dict[str, Any]) -> Event:
    parents = tuple(sorted(parents))
    return Event(id=compute_id(type, parents, payload), type=type, parents=parents, payload=payload)


def verify(event: Event) -> bool:
    """True iff the event's id matches its content."""
    return event.id == compute_id(event.type, event.parents, event.payload)
