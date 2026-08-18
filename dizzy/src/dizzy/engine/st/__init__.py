"""The single-process engine's scheduling shell.

Two pieces, both stdlib-only — this shell adds no dependencies at all:

- ``command_queue`` — ``DurableCommandQueue``: a sqlite command queue with
  atomic claim and lanes. Queued work survives a crash or a redeploy, which is
  the whole reason it isn't a ``queue.Queue``.
- ``bus`` — ``TelemetryBus``: a bounded in-process ring the host reads to show
  what the engine is doing. Deliberately ephemeral; telemetry is observation,
  never truth. The event store is the truth.

Everything app-shaped is injected: ``registry`` (command class name → class,
for rehydrating a claimed row), ``lane_of`` (which lane a command runs in) and
``label_fields`` (which payload fields make a readable job label). The
module-level defaults are fallbacks for a host that doesn't care, not
knowledge about any particular feature.

The worker loop that drives claim → run_command → ack is the host's: this
shell owns the queue, not the threads.
"""

from dizzy.engine.st.bus import TelemetryBus
from dizzy.engine.st.command_queue import DEFAULT_LABEL_FIELDS, DurableCommandQueue

__all__ = ["DEFAULT_LABEL_FIELDS", "DurableCommandQueue", "TelemetryBus"]
