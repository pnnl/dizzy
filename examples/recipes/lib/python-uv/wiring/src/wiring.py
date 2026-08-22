# AUTO-GENERATED — do not edit. Regenerate with `dizzy generate wiring`.
"""Wiring for the recipes feature: elements bound to a DIZZY engine.

Generated from recipes.feat.yaml. Every registration below is a line of that file read
back out, which is why editing this module is the wrong move — the next
regeneration discards it. To specialize one element's binding, pass an override:

    build_engine(queue, store, Resources(adapters=..., overrides={"record_ingredient": my_runner}))

The wiring is engine-mediated. A procedure's emitters go to ``engine.emit_event``
and a policy's to ``engine.dispatch_command``, so no element ever calls another;
the engine appends each event, folds its projections, commits, and only then lets
policies dispatch. A dispatched command lands on the queue as the next unit of
work — draining that queue is the scheduling shell's job, not this module's.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dizzy.engine import (
    Engine,
    EventStore,
    FeatGraph,
    HostApp,
    Runtime,
    ShellServices,
)

from gen_def.pydantic.commands import (
    AddRecipeStep,
    AddStepInput,
    AdvanceBatch,
    DefineRecipe,
    RegisterIngredient,
    RegisterTool,
    StartBatch,
)
from gen_def.pydantic.events import (
    BatchCompleted,
    BatchOpened,
    BatchRunFailed,
    EntityConsumed,
    EntityDerived,
    EntityProduced,
    IngredientRegistered,
    RecipeDefined,
    RecipeStepAdded,
    StepInputAdded,
    ToolRegistered,
)
from gen_int.python.procedure.record_ingredient_context import (
    record_ingredient_context,
    record_ingredient_emitters,
)
from gen_int.python.procedure.record_tool_context import (
    record_tool_context,
    record_tool_emitters,
)
from gen_int.python.procedure.record_recipe_context import (
    record_recipe_context,
    record_recipe_emitters,
)
from gen_int.python.procedure.record_step_context import (
    record_step_context,
    record_step_emitters,
)
from gen_int.python.procedure.record_step_input_context import (
    record_step_input_context,
    record_step_input_emitters,
)
from gen_int.python.procedure.open_batch_context import (
    open_batch_context,
    open_batch_emitters,
    open_batch_queries,
)
from gen_int.python.procedure.run_batch_context import (
    run_batch_context,
    run_batch_emitters,
    run_batch_queries,
)
from gen_int.python.policy.advance_ready_batches_context import (
    advance_ready_batches_context,
    advance_ready_batches_emitters,
    advance_ready_batches_queries,
)
from gen_int.python.projection.ingredient_catalog_projection import (
    ingredient_catalog_context,
)
from gen_int.python.projection.tool_catalog_projection import (
    tool_catalog_context,
)
from gen_int.python.projection.recipe_catalog_projection import (
    recipe_catalog_context,
)
from gen_int.python.projection.step_catalog_projection import (
    step_catalog_context,
)
from gen_int.python.projection.step_input_catalog_projection import (
    step_input_catalog_context,
)
from gen_int.python.projection.batch_store_projection import (
    batch_store_context,
)
from gen_int.python.projection.batch_finalizer_projection import (
    batch_finalizer_context,
)
from gen_int.python.projection.batch_reblocker_projection import (
    batch_reblocker_context,
)
from gen_int.python.projection.inventory_store_projection import (
    inventory_store_context,
)
from gen_int.python.projection.inventory_consumer_projection import (
    inventory_consumer_context,
)
from gen_int.python.projection.generation_graph_projection import (
    generation_graph_context,
)
from gen_int.python.projection.derivation_graph_projection import (
    derivation_graph_context,
)
from gen_int.python.query.get_recipe import (
    get_recipe_context,
)
from gen_int.python.query.get_recipe_steps import (
    get_recipe_steps_context,
)
from gen_int.python.query.get_step_inputs import (
    get_step_inputs_context,
)
from gen_int.python.query.get_batch import (
    get_batch_context,
)
from gen_int.python.query.check_inventory import (
    check_inventory_context,
)
from gen_int.python.query.find_blocked_batches import (
    find_blocked_batches_context,
)
from gen_int.python.query.trace_provenance import (
    trace_provenance_context,
)
from gen_int.python.query.list_recipes import (
    list_recipes_context,
)
from gen_int.python.query.list_batches import (
    list_batches_context,
)

# The element implementations — each is its own workspace package.
from record_ingredient import record_ingredient
from record_tool import record_tool
from record_recipe import record_recipe
from record_step import record_step
from record_step_input import record_step_input
from open_batch import open_batch
from run_batch import run_batch
from advance_ready_batches import advance_ready_batches
from ingredient_catalog import ingredient_catalog
from tool_catalog import tool_catalog
from recipe_catalog import recipe_catalog
from step_catalog import step_catalog
from step_input_catalog import step_input_catalog
from batch_store import batch_store
from batch_finalizer import batch_finalizer
from batch_reblocker import batch_reblocker
from inventory_store import inventory_store
from inventory_consumer import inventory_consumer
from generation_graph import generation_graph
from derivation_graph import derivation_graph
from get_recipe import get_recipe
from get_recipe_steps import get_recipe_steps
from get_step_inputs import get_step_inputs
from get_batch import get_batch
from check_inventory import check_inventory
from find_blocked_batches import find_blocked_batches
from trace_provenance import trace_provenance
from list_recipes import list_recipes
from list_batches import list_batches

@dataclass
class Resources:
    """What a host supplies so the declared elements can actually run."""

    adapters: Mapping[str, Any] = field(default_factory=dict)
    """Adapter instances by name: ``sqla``."""

    env: Any = None
    """Unused: this feature declares no environment."""

    telemetry: Any = None
    """Unused: this feature declares no telemetry."""

    overrides: Mapping[str, Callable] = field(default_factory=dict)
    """Element name -> replacement runner. The supported way to specialize a
    binding: a capability-pooled element a node must not import, a query that
    needs a short-lived session. Forking this module to get the same effect
    throws away the guarantee that it matches the feat."""

    adapter_for: Callable[[str, Any], Any] | None = None
    """Optional per-call adapter factory, ``(adapter_name, ingested_at) ->
    adapter``. Supply it when a projection must see the event's stream-append
    time; otherwise the static instance from ``adapters`` is used for every
    fold."""

    def adapter(self, name: str, ingested_at: Any = None) -> Any:
        if self.adapter_for is not None:
            return self.adapter_for(name, ingested_at)
        return self.adapters[name]

@dataclass
class Queries:
    """Every declared query, bound to the read adapter.

    Procedures and policies receive these through their generated context, so
    a query is called with its input alone — the adapter is already bound."""

    get_recipe: Callable[[Any], Any]
    get_recipe_steps: Callable[[Any], Any]
    get_step_inputs: Callable[[Any], Any]
    get_batch: Callable[[Any], Any]
    check_inventory: Callable[[Any], Any]
    find_blocked_batches: Callable[[Any], Any]
    trace_provenance: Callable[[Any], Any]
    list_recipes: Callable[[Any], Any]
    list_batches: Callable[[Any], Any]


def build_queries(resources: Resources) -> Queries:
    """Bind every querier over the read adapter its model declares."""
    def _get_recipe(inp: Any) -> Any:
        return get_recipe(
            inp,
            get_recipe_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    def _get_recipe_steps(inp: Any) -> Any:
        return get_recipe_steps(
            inp,
            get_recipe_steps_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    def _get_step_inputs(inp: Any) -> Any:
        return get_step_inputs(
            inp,
            get_step_inputs_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    def _get_batch(inp: Any) -> Any:
        return get_batch(
            inp,
            get_batch_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    def _check_inventory(inp: Any) -> Any:
        return check_inventory(
            inp,
            check_inventory_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    def _find_blocked_batches(inp: Any) -> Any:
        return find_blocked_batches(
            inp,
            find_blocked_batches_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    def _trace_provenance(inp: Any) -> Any:
        return trace_provenance(
            inp,
            trace_provenance_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    def _list_recipes(inp: Any) -> Any:
        return list_recipes(
            inp,
            list_recipes_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    def _list_batches(inp: Any) -> Any:
        return list_batches(
            inp,
            list_batches_context(
                adapter=resources.adapter("sqla"),
            ),
        )

    return Queries(
        get_recipe=resources.overrides.get("get_recipe", _get_recipe),
        get_recipe_steps=resources.overrides.get("get_recipe_steps", _get_recipe_steps),
        get_step_inputs=resources.overrides.get("get_step_inputs", _get_step_inputs),
        get_batch=resources.overrides.get("get_batch", _get_batch),
        check_inventory=resources.overrides.get("check_inventory", _check_inventory),
        find_blocked_batches=resources.overrides.get("find_blocked_batches", _find_blocked_batches),
        trace_provenance=resources.overrides.get("trace_provenance", _trace_provenance),
        list_recipes=resources.overrides.get("list_recipes", _list_recipes),
        list_batches=resources.overrides.get("list_batches", _list_batches),
    )

def build_projection_runners(
    resources: Resources,
) -> dict[type, list[tuple[str, Callable]]]:
    """Event class -> ``[(name, runner)]``, the shape the engine registers.

    The same map drives ``dizzy.engine.rebuild.rebuild`` and
    ``replicate.fold_envelopes``, so one registration serves all three
    triggers and a refold cannot fold a different set than the engine."""
    def _ingredient_catalog(event: Any, ingested_at: Any = None) -> None:
        ingredient_catalog(
            event,
            ingredient_catalog_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _tool_catalog(event: Any, ingested_at: Any = None) -> None:
        tool_catalog(
            event,
            tool_catalog_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _recipe_catalog(event: Any, ingested_at: Any = None) -> None:
        recipe_catalog(
            event,
            recipe_catalog_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _step_catalog(event: Any, ingested_at: Any = None) -> None:
        step_catalog(
            event,
            step_catalog_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _step_input_catalog(event: Any, ingested_at: Any = None) -> None:
        step_input_catalog(
            event,
            step_input_catalog_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _batch_store(event: Any, ingested_at: Any = None) -> None:
        batch_store(
            event,
            batch_store_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _batch_finalizer(event: Any, ingested_at: Any = None) -> None:
        batch_finalizer(
            event,
            batch_finalizer_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _batch_reblocker(event: Any, ingested_at: Any = None) -> None:
        batch_reblocker(
            event,
            batch_reblocker_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _inventory_store(event: Any, ingested_at: Any = None) -> None:
        inventory_store(
            event,
            inventory_store_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _inventory_consumer(event: Any, ingested_at: Any = None) -> None:
        inventory_consumer(
            event,
            inventory_consumer_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _generation_graph(event: Any, ingested_at: Any = None) -> None:
        generation_graph(
            event,
            generation_graph_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    def _derivation_graph(event: Any, ingested_at: Any = None) -> None:
        derivation_graph(
            event,
            derivation_graph_context(
                adapter=resources.adapter("sqla", ingested_at),
            ),
        )

    runners: dict[type, list[tuple[str, Callable]]] = {}
    runners.setdefault(IngredientRegistered, []).append(("ingredient_catalog", resources.overrides.get("ingredient_catalog", _ingredient_catalog)))
    runners.setdefault(ToolRegistered, []).append(("tool_catalog", resources.overrides.get("tool_catalog", _tool_catalog)))
    runners.setdefault(RecipeDefined, []).append(("recipe_catalog", resources.overrides.get("recipe_catalog", _recipe_catalog)))
    runners.setdefault(RecipeStepAdded, []).append(("step_catalog", resources.overrides.get("step_catalog", _step_catalog)))
    runners.setdefault(StepInputAdded, []).append(("step_input_catalog", resources.overrides.get("step_input_catalog", _step_input_catalog)))
    runners.setdefault(BatchOpened, []).append(("batch_store", resources.overrides.get("batch_store", _batch_store)))
    runners.setdefault(BatchCompleted, []).append(("batch_finalizer", resources.overrides.get("batch_finalizer", _batch_finalizer)))
    runners.setdefault(BatchRunFailed, []).append(("batch_reblocker", resources.overrides.get("batch_reblocker", _batch_reblocker)))
    runners.setdefault(EntityProduced, []).append(("inventory_store", resources.overrides.get("inventory_store", _inventory_store)))
    runners.setdefault(EntityConsumed, []).append(("inventory_consumer", resources.overrides.get("inventory_consumer", _inventory_consumer)))
    runners.setdefault(EntityProduced, []).append(("generation_graph", resources.overrides.get("generation_graph", _generation_graph)))
    runners.setdefault(EntityDerived, []).append(("derivation_graph", resources.overrides.get("derivation_graph", _derivation_graph)))
    return runners

def build_engine(
    command_queue: Any,
    store: EventStore,
    resources: Resources,
    observer: Callable[[str, Any], None] | None = None,
    commit: Callable[[], None] | None = None,
    bus: Callable[[dict], None] | None = None,
    otel: Any = None,
) -> Engine:
    """Register every declared element and return the engine.

    *command_queue* is the shell's: dispatches leave through it, and draining
    it is the shell's job. *commit* is the read-model transaction boundary the
    engine owns — it fires once per event, after the fold, before policies."""
    engine = Engine(
        command_queue=command_queue,
        store=store,
        observer=observer,
        commit=commit,
        bus=bus,
        otel=otel,
    )
    queries = build_queries(resources)

    # Data loop: one registration per declared event: projection edge.
    for event_class, runners in build_projection_runners(resources).items():
        for name, runner in runners:
            engine.register_projection(event_class, runner, name=name)

    # Reactivity loop: a policy's emits are COMMANDS, dispatched.
    def _advance_ready_batches(event: Any) -> None:
        advance_ready_batches(
            event,
            advance_ready_batches_context(
                emit=advance_ready_batches_emitters(
                    advance_batch=engine.dispatch_command,
                ),
                query=advance_ready_batches_queries(
                    find_blocked_batches=queries.find_blocked_batches,
                ),
            ),
        )

    engine.register_policy(
        EntityProduced,
        resources.overrides.get("advance_ready_batches", _advance_ready_batches),
        name="advance_ready_batches",
    )

    # Command handlers: a procedure's emits are EVENTS, appended.
    def _record_ingredient(command: Any) -> None:
        record_ingredient(
            record_ingredient_context(
                emit=record_ingredient_emitters(
                    ingredient_registered=engine.emit_event,
                ),
            ),
            command,
        )

    engine.register_procedure(
        RegisterIngredient,
        resources.overrides.get("record_ingredient", _record_ingredient),
        name="record_ingredient",
    )
    def _record_tool(command: Any) -> None:
        record_tool(
            record_tool_context(
                emit=record_tool_emitters(
                    tool_registered=engine.emit_event,
                ),
            ),
            command,
        )

    engine.register_procedure(
        RegisterTool,
        resources.overrides.get("record_tool", _record_tool),
        name="record_tool",
    )
    def _record_recipe(command: Any) -> None:
        record_recipe(
            record_recipe_context(
                emit=record_recipe_emitters(
                    recipe_defined=engine.emit_event,
                ),
            ),
            command,
        )

    engine.register_procedure(
        DefineRecipe,
        resources.overrides.get("record_recipe", _record_recipe),
        name="record_recipe",
    )
    def _record_step(command: Any) -> None:
        record_step(
            record_step_context(
                emit=record_step_emitters(
                    recipe_step_added=engine.emit_event,
                ),
            ),
            command,
        )

    engine.register_procedure(
        AddRecipeStep,
        resources.overrides.get("record_step", _record_step),
        name="record_step",
    )
    def _record_step_input(command: Any) -> None:
        record_step_input(
            record_step_input_context(
                emit=record_step_input_emitters(
                    step_input_added=engine.emit_event,
                ),
            ),
            command,
        )

    engine.register_procedure(
        AddStepInput,
        resources.overrides.get("record_step_input", _record_step_input),
        name="record_step_input",
    )
    def _open_batch(command: Any) -> None:
        open_batch(
            open_batch_context(
                emit=open_batch_emitters(
                    batch_opened=engine.emit_event,
                ),
                query=open_batch_queries(
                    get_recipe=queries.get_recipe,
                    check_inventory=queries.check_inventory,
                ),
            ),
            command,
        )

    engine.register_procedure(
        StartBatch,
        resources.overrides.get("open_batch", _open_batch),
        name="open_batch",
    )
    def _run_batch(command: Any) -> None:
        run_batch(
            run_batch_context(
                emit=run_batch_emitters(
                    step_performed=engine.emit_event,
                    entity_consumed=engine.emit_event,
                    entity_produced=engine.emit_event,
                    entity_derived=engine.emit_event,
                    batch_completed=engine.emit_event,
                    batch_run_failed=engine.emit_event,
                ),
                query=run_batch_queries(
                    get_batch=queries.get_batch,
                    get_recipe=queries.get_recipe,
                    get_recipe_steps=queries.get_recipe_steps,
                    get_step_inputs=queries.get_step_inputs,
                    check_inventory=queries.check_inventory,
                ),
            ),
            command,
        )

    engine.register_procedure(
        AdvanceBatch,
        resources.overrides.get("run_batch", _run_batch),
        name="run_batch",
    )

    return engine

FEAT_FILE = Path(__file__).parent / "recipes.feat.yaml"
"""The feature-file this wiring was generated from, shipped inside the
package so a lifted-out ``lib/`` stays self-contained. It is a build
artifact: regenerating the wiring refreshes it."""


def feat_graph() -> FeatGraph:
    """This feature's topology, every declared name resolved to its class."""
    return FeatGraph.load(FEAT_FILE)


def build_runtime(
    services: ShellServices,
    resources: Resources,
    store: EventStore | None = None,
    commit: Callable[[], None] | None = None,
    session: Any = None,
    refresh: Callable[[], None] = lambda: None,
) -> Runtime:
    """Build this process's engine around the services a shell provides.

    The shell's observer is chained in, not replaced: a shell collects the
    events a command emitted through it, so an app that installs its own
    observer without chaining silently unplugs the shell."""
    engine = build_engine(
        services.command_queue,
        store if store is not None else EventStore(graph=feat_graph()),
        resources,
        observer=services.observer,
        commit=commit,
    )
    return Runtime(engine=engine, session=session, refresh=refresh)


def host_app(
    build: Callable[[ShellServices], Runtime],
    **kwargs: Any,
) -> HostApp:
    """Package a runtime builder as the HostApp a shell resolves from
    ``$DIZZY_HOST_APP``. *build* is usually ``build_runtime`` with this host's
    resources already bound::

        app = host_app(lambda services: build_runtime(services, my_resources()))

    Extra keyword arguments (``routes``, ``otel``, ``on_command_done``, …) pass
    straight through to ``HostApp``."""
    return HostApp(graph=feat_graph(), build_runtime=build, **kwargs)
