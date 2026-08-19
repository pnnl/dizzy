"""Host layer for the recipes feature, shared by every host that runs it.

**The wiring is generated.** `dizzy generate wiring` reads `recipes.feat.yaml`
and emits `lib/python-uv/wiring/` — the module that registers each procedure
under the command it handles, each projection under the event it folds, and the
policy under the event it reacts to, binding every emitter to the engine. That
used to be ~250 lines of hand-written routing in this file, and it was the last
artifact in the toolchain still copied out of the design by hand.

What is left here is what a host genuinely owns and the feat cannot know: which
database, which adapter instance, and how commands get drained. `demo.py` (a CLI
host) and `server.py` (an HTTP host) both call :func:`build_kitchen`.

A :class:`Kitchen` bundles ready-to-call command runners and query callables
bound to one ``SqlaAdapter`` (i.e. one database session). Build a fresh one per
unit of work (per CLI run, per HTTP request). Calling a command runner runs it
*to quiescence*: the command, then every event it causes, then every command
those events dispatch.

The optional ``observer`` is called with ``(event_name, event)`` for every event
the feature emits — hosts use it to log, print, or collect the cascade.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional

import wiring
from gen_int.python.adapters.sqla import SqlaAdapter

EventObserver = Callable[[str, Any], None]


class CommandQueue:
    """The external command queue, in-process.

    A deployed host swaps this for a real one — ``dizzy.engine.st``'s durable
    sqlite queue, or ``mp``'s broker — and nothing else changes, because the
    engine only ever calls ``put``.
    """

    def __init__(self) -> None:
        self._items: deque = deque()

    def put(self, command: Any, origin: str = "policy") -> None:
        self._items.append(command)

    def get(self) -> Any:
        return self._items.popleft()

    def qsize(self) -> int:
        return len(self._items)


@dataclass
class Kitchen:
    """Command runners and query callables bound to one database session."""

    engine: Any
    """The engine underneath, for hosts that want the event store or the topology."""

    # Commands. Every one runs its procedure and then drains the cascade, so the
    # attribute names below are a convenience over a single `run` — they exist so
    # a host reads as `kitchen.start_batch(...)` rather than `kitchen.run(...)`.
    register_ingredient: Callable[[Any], None]
    register_tool: Callable[[Any], None]
    define_recipe: Callable[[Any], None]
    add_recipe_step: Callable[[Any], None]
    add_step_input: Callable[[Any], None]
    start_batch: Callable[[Any], None]
    advance_batch: Callable[[Any], None]

    # Queries, bound by the generated wiring over the read adapter.
    get_recipe: Callable[[Any], Any]
    get_recipe_steps: Callable[[Any], Any]
    get_step_inputs: Callable[[Any], Any]
    get_batch: Callable[[Any], Any]
    check_inventory: Callable[[Any], Any]
    find_blocked_batches: Callable[[Any], Any]
    trace_provenance: Callable[[Any], Any]
    list_recipes: Callable[[Any], Any]
    list_batches: Callable[[Any], Any]


def build_kitchen(
    adapter: SqlaAdapter,
    observer: Optional[EventObserver] = None,
    store: Optional[Any] = None,
    command_queue: Optional[CommandQueue] = None,
    commit: Optional[Callable[[], None]] = None,
) -> Kitchen:
    """Bind the generated wiring to this host's database and return a Kitchen.

    *store* defaults to an in-memory event stream, so an example or a test gets
    the real append-fold-dispatch path without leaving a file behind; a deployed
    host passes one backed by a path. *commit* defaults to committing the
    adapter's session once per event — the boundary the engine owns, which is
    what makes fold-then-dispatch true for the next reader.
    """
    resources = wiring.Resources(adapters={"sqla": adapter})
    store = store if store is not None else wiring.EventStore(
        path=":memory:", graph=wiring.feat_graph()
    )
    queue = command_queue if command_queue is not None else CommandQueue()

    engine = wiring.build_engine(
        queue,
        store,
        resources,
        observer=observer,
        commit=commit if commit is not None else adapter.session.commit,
    )
    queries = wiring.build_queries(resources)

    def run_to_quiescence(command: Any) -> None:
        """Run one command, then every command its events dispatched.

        The engine drains the EVENT queue itself; draining the COMMAND queue is
        the scheduling shell's job, and in a single-process host that job is a
        while-loop. Under ``dizzy.engine.st`` it is a worker claiming rows;
        under ``mp`` it is N processes. Same generated wiring either way.
        """
        engine.run_command(command)
        while queue.qsize():
            engine.run_command(queue.get())

    return Kitchen(
        engine=engine,
        register_ingredient=run_to_quiescence,
        register_tool=run_to_quiescence,
        define_recipe=run_to_quiescence,
        add_recipe_step=run_to_quiescence,
        add_step_input=run_to_quiescence,
        start_batch=run_to_quiescence,
        advance_batch=run_to_quiescence,
        get_recipe=queries.get_recipe,
        get_recipe_steps=queries.get_recipe_steps,
        get_step_inputs=queries.get_step_inputs,
        get_batch=queries.get_batch,
        check_inventory=queries.check_inventory,
        find_blocked_batches=queries.find_blocked_batches,
        trace_provenance=queries.trace_provenance,
        list_recipes=queries.list_recipes,
        list_batches=queries.list_batches,
    )
