"""A host whose command queue is built at MODULE SCOPE, run as ``__main__``.

The shape ``_is_host_process()`` exists to protect, and the one a helper called
from inside a child target cannot reproduce: ``multiprocessing`` assigns
``_parent_process`` in ``BaseProcess._bootstrap``, which runs AFTER the spawned
child re-imports this file as ``__mp_main__``. A queue constructed here is
therefore constructed in the window where the child still looks like a host.

Run as a subprocess by ``test_st_executor.py``; each construction appends one
``<__name__> recovered=<n> pool_child=<bool>`` line to ``$DIZZY_PROBE_OUT``.

``pool_child`` is the same window seen from the store's side: ``in_pool_child()``
decides whether an ``EventStore`` built here takes a read-write connection on
the host's log, and it must already say "child" during the re-import — the
marker ``mark_pool_child()`` sets does not exist until the child's task runs.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from dizzy.engine.ports import in_pool_child  # noqa: E402
from dizzy.engine.st import DurableCommandQueue  # noqa: E402

from _pool_app import COMMAND_CLASSES, PoolPing  # noqa: E402  (isort: skip)

_QUEUE = DurableCommandQueue(registry=COMMAND_CLASSES, path=os.environ["DIZZY_PROBE_DB"])
with open(os.environ["DIZZY_PROBE_OUT"], "a", encoding="utf-8") as fh:
    fh.write(f"{__name__} recovered={_QUEUE.recovered} pool_child={in_pool_child()}\n")
_QUEUE.close()


def _child() -> None:
    """Nothing: the damage, if any, is already done by the re-import above."""


if __name__ == "__main__":
    # Leave a row 'running' ACROSS the spawn. Seeding it before this point
    # would let the module-scope construction above recover it, and the child
    # would then re-import a queue with nothing left to take — which is how the
    # first version of this probe passed against the very bug it targets.
    host = DurableCommandQueue(registry=COMMAND_CLASSES, path=os.environ["DIZZY_PROBE_DB"])
    host.put(PoolPing(key="in-flight"), origin="external")
    assert host.claim(timeout=5) is not None
    host.close()

    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_child)
    proc.start()
    proc.join(timeout=60.0)
