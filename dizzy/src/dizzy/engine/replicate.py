"""Anti-entropy replication + fold-on-replicate.

Pull-based, git-style: fetch the peer's heads, walk parent pointers backwards
until events we already hold (the stop-at-known idiom), verify every record's
content hash on arrival, add parents-first, then fold each NEW event through
the same projections local emits use — one code path, two triggers. The
event's ``ingested_at`` travels inside its hashed payload, so both time axes
survive the hop; replay equality across nodes follows from canonical order.

Transports are closures — ``fetch_heads() -> [id]`` and ``fetch_event(id) ->
Event`` — so a peer can be a file on the same disk, a USB stick, an NFS mount
or an HTTP endpoint without this module knowing which. Fetching is naive and
per-event; batching is an optimization for later.

Nothing here names a feature. :func:`fold_envelopes` takes the projection
runners and the reconstructor as arguments, because *which* projections fold
an event is the wiring's knowledge, not the replicator's.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from dizzy.engine.dagstore import DagStore, Event
from dizzy.engine.store import Envelope, EventStore


def pull(
    local: EventStore,
    fetch_heads: Callable[[], Iterable[str]],
    fetch_event: Callable[[str], Event],
) -> list[Envelope]:
    """Pull everything the peer has that we lack.

    Returns the new envelopes in delivery order (parents-first), ready for
    :func:`fold_envelopes`.
    """
    fetched: dict[str, Event] = {}
    frontier = [h for h in fetch_heads() if h not in local]
    while frontier:
        event_id = frontier.pop()
        if event_id in fetched or event_id in local:
            continue
        event = fetch_event(event_id)  # the hash is verified on arrival
        fetched[event_id] = event
        frontier.extend(event.parents)

    added: list[Envelope] = []
    pending = dict(fetched)
    while pending:
        ready = [e for e in pending.values() if all(p not in pending for p in e.parents)]
        if not ready:
            raise RuntimeError("cycle in fetched subset — hashes forbid this")
        for event in sorted(ready, key=lambda e: e.id):
            added.append(local.add_replicated(event))
            del pending[event.id]
    return added


def fold_envelopes(
    envelopes: Iterable[Envelope],
    session: Any,
    runners: Mapping[Any, list],
    reconstruct: Callable[[Envelope], Any],
) -> int:
    """Fold-on-replicate: replicated events run the SAME projections local
    emits do, with their original ``ingested_at``.

    Confluent projections make arrival order and redelivery harmless, which is
    what lets a pull happen at any time. Returns the number of events folded.

    *runners* maps event class -> ``[(name, runner)]``, the same shape the
    engine registers; *reconstruct* turns an envelope back into an event
    instance (``EventStore.reconstruct_event`` is one).
    """
    folded = 0
    for envelope in envelopes:
        try:
            event = reconstruct(envelope)
        except KeyError:
            continue  # retired type: the fact replicates, nothing folds it
        for _name, runner in runners.get(type(event), []):
            runner(event, envelope.ingested_at)
        # Commit PER EVENT, mirroring the engine's boundary: these events are
        # already in the local DAG, so the next pull will NOT re-fetch them —
        # a crash mid-batch must not strand appended-but-unfolded facts.
        session.commit()
        folded += 1
    return folded


# ── Transports ──────────────────────────────────────────────────────────────


def file_transport(peer_store_path: str):
    """Read a peer's store file directly (same disk / USB / NFS)."""
    peer = DagStore(peer_store_path)
    return peer.heads, peer.get


def http_transport(base_url: str, client: Any = None):
    """Speak to a peer over HTTP.

    The peer serves two endpoints, which a host mounts in whatever framework it
    already runs — DIZZY does not ship a server, and would have to pick one:

    ``GET {base}/replicate/heads``
        ``{"heads": [<event id>, ...]}`` — from :meth:`EventStore.heads`.

    ``GET {base}/replicate/event/{id}``
        ``{"id", "type", "parents", "payload"}`` — from
        :meth:`EventStore.raw_event`, which returns exactly those fields.
        404 when the id is unknown.

    *client* is injectable for tests — anything with ``.get(url)`` returning a
    response with ``.json()``. httpx is imported only if you do not supply one,
    so it is the caller's dependency, not DIZZY's.
    """
    if client is None:
        import httpx

        client = httpx.Client(timeout=30)

    def fetch_heads():
        return client.get(f"{base_url}/replicate/heads").json()["heads"]

    def fetch_event(event_id: str) -> Event:
        d = client.get(f"{base_url}/replicate/event/{event_id}").json()
        return Event(id=d["id"], type=d["type"], parents=tuple(d["parents"]), payload=d["payload"])

    return fetch_heads, fetch_event
