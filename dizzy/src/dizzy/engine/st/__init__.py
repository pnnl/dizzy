"""The single-process engine's scheduling shell.

Two pieces, both stdlib-only — this shell adds no dependencies at all:

- ``command_queue`` — ``DurableCommandQueue``: a sqlite command queue with
  atomic claim, lanes and a two-level priority. Queued work survives a crash or
  a redeploy, which is the whole reason it isn't a ``queue.Queue``.
- ``executor`` — ``LaneExecutor``: the worker loop, promoted out of the host
  and parameterised by the generated placement (``LANES``). One main lane
  serving every ``inline`` group in the calling thread, plus one background
  lane per ``pool`` group backed by a spawned process pool. The log keeps
  exactly one writer, and a host with no topology gets the single-lane loop it
  had before, spawning nothing.
- ``bus`` — ``TelemetryBus``: a bounded in-process ring the host reads to show
  what the engine is doing. Deliberately ephemeral; telemetry is observation,
  never truth. The event store is the truth.

Everything app-shaped is injected: ``registry`` (command class name → class,
for rehydrating a claimed row), ``lane_of`` (which lane a command runs in),
``priority_of`` (batch or interactive) and ``label_fields`` (which payload
fields make a readable job label). The
module-level defaults are fallbacks for a host that doesn't care, not
knowledge about any particular feature.

A host may still drive ``claim → run_command → ack`` itself — nothing forces
:class:`~dizzy.engine.st.executor.LaneExecutor` on it — but a host that wants
more than one lane no longer has to write the threading.

**The generated wiring builds both.** A ``dizzy generate wiring`` run over a
feature with a ``topology.yaml`` emits ``build_command_queue()`` and
``build_lane_executor(queue, engine)`` beside the ``LANES`` table, binding
``lane_of``/``priority_of``/``lanes`` so a host never restates its own
deployment. Both import this package function-locally, which is what keeps a
wiring shell-neutral. What stays the host's is the LIFECYCLE — when to start,
pump and stop — and the dials the topology does not state (``ordered``,
``backlog``, ``poll_interval``).
"""

from dizzy.engine.st.bus import TelemetryBus
from dizzy.engine.st.command_queue import (
    DEFAULT_LABEL_FIELDS,
    PRIORITY_BATCH,
    PRIORITY_INTERACTIVE,
    DurableCommandQueue,
)
from dizzy.engine.st.executor import (
    DEFAULT_LANES,
    Completion,
    LaneExecutor,
    ProcedureResult,
    run_procedure_job,
    spawn_pool,
)

__all__ = [
    "DEFAULT_LABEL_FIELDS",
    "DEFAULT_LANES",
    "PRIORITY_BATCH",
    "PRIORITY_INTERACTIVE",
    "Completion",
    "DurableCommandQueue",
    "LaneExecutor",
    "ProcedureResult",
    "TelemetryBus",
    "run_procedure_job",
    "spawn_pool",
]
