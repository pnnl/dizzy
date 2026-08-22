"""The DIZZY runtime kit's engine layer — how a declared feature gets RUN.

``dizzy generate`` turns a ``.feat.yaml`` into schemas, contracts and stubs.
This package is the other half: the machinery that schedules those elements at
runtime, and — crucially — does so without knowing what they are.

Five pieces here, plus a scheduling shell per execution model:

- ``loop`` — ``Engine``: the control loop. Command -> procedure -> events ->
  projections -> policies -> commands, with the read-model commit boundary
  between the fold and the dispatch. It is keyed by generated class, so it
  names nothing.
- ``store`` — ``EventStore`` over ``dagstore``, a content-addressed event DAG:
  the truth an engine appends to before anything else runs.
- ``rebuild`` / ``replicate`` — the two things you can do with a stream besides
  run it forward: refold it into the read models (the recoverability test), and
  pull a peer's facts and fold those through the same projections. Both take
  the projection runners as an argument, so neither knows a feature.
- ``registry`` — ``FeatGraph``: the feat file read into an app's topology, with
  every declared command and event resolved to its generated pydantic class by
  DIZZY's naming convention. This is what makes a shell generic. The feat
  already declares everything; a shell that hard-codes any of it has copied
  the design out of the artifact, which is the one thing DIZZY exists to
  prevent.
- ``ports`` — ``HostApp`` / ``ShellServices`` / ``Runtime`` and the
  ``CommandQueue`` / ``TelemetryBus`` protocols: the seam through which
  everything app-specific reaches a shell. An app publishes one ``HostApp``;
  a shell resolves it from ``$DIZZY_HOST_APP`` and needs nothing else.

Scheduling shells (installed via extras, so a host pays only for the one it
runs):

- ``dizzy.engine.st`` — single process: a durable sqlite command queue with
  atomic claim + lanes, and an in-process telemetry ring. Stdlib only.
- ``dizzy.engine.mp`` — a fleet: Dramatiq/Redis workers, pool-routed, with
  telemetry over Redis pub/sub. Needs ``dizzy[mp]``.

The shells differ ONLY in scheduling — who holds the command queue, who runs
the workers, where telemetry lands. The engine they drive and the wiring that
binds a feature to it are shared, and both shells execute them verbatim.

They do NOT, however, claim the same semantics, and that difference is
deliberate rather than incidental. Because the engine hands every
policy-dispatched command to the shell's queue, the shell owns the command
phase — so it is the shell, not the engine, that decides how many commands run
at once. ``st`` drains one lane in one process and is sequentially consistent:
one legal interleaving, which is DIZZY's *defined* semantics. ``mp`` runs N
workers under at-least-once delivery and is knowingly weaker, relying on
confluent projections to absorb the reordering and the duplicates. A host picks
the shell whose guarantee it needs.

The engine reads and writes read models only through the runners the wiring
registers, so it carries no ORM: ``dizzy.engine`` costs pyyaml and pydantic and
nothing else. The mp shell's broker dependencies stay behind its extra, so
importing ``dizzy.engine`` never drags in a broker.
"""

from dizzy.engine.loop import Engine
from dizzy.engine.ports import (
    CommandQueue,
    HostApp,
    NullOtel,
    Runtime,
    ShellServices,
    TelemetryBus,
    chain_observers,
    null_app,
)
from dizzy.engine.registry import (
    SECTIONS,
    TOPOLOGY_SECTIONS,
    FeatGraph,
    camel_case,
    check_name,
    find_feat,
    graph,
    reset_graph,
    snake_case,
)
from dizzy.engine.store import Envelope, EventStore, reconstruct_event

__all__ = [
    "SECTIONS",
    "TOPOLOGY_SECTIONS",
    "CommandQueue",
    "Engine",
    "Envelope",
    "EventStore",
    "FeatGraph",
    "HostApp",
    "NullOtel",
    "Runtime",
    "ShellServices",
    "TelemetryBus",
    "camel_case",
    "chain_observers",
    "check_name",
    "find_feat",
    "graph",
    "null_app",
    "reconstruct_event",
    "reset_graph",
    "snake_case",
]
