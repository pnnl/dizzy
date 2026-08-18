"""The DIZZY runtime kit's engine layer — how a declared feature gets RUN.

``dizzy generate`` turns a ``.feat.yaml`` into schemas, contracts and stubs.
This package is the other half: the machinery that schedules those elements at
runtime, and — crucially — does so without knowing what they are.

Two pieces here, plus a scheduling shell per execution model:

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

``ports`` and ``registry`` import at the cost of pyyaml; the mp shell's broker
dependencies stay behind its extra, so importing ``dizzy.engine`` never drags
in a broker.
"""

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

__all__ = [
    "SECTIONS",
    "TOPOLOGY_SECTIONS",
    "CommandQueue",
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
    "reset_graph",
    "snake_case",
]
