"""Host wiring for the recipes feature, shared by every host that runs it.

Dizzy generates the typed pieces (commands, events, procedures, projections,
queries, the policy) and :mod:`dizzy.engine` runs them; a *host* owns the
database and connects the two. That connection is this file: it registers each
element with the engine under the command or event the feat declares for it,
and binds each query over the read adapter. ``demo.py`` (a CLI host) and
``server.py`` (an HTTP host) both call :func:`build_kitchen` rather than repeat
it.

**Engine-mediated, not element-to-element.** Every emitted event goes to
``engine.emit_event`` and every dispatched command to ``engine.dispatch_command``
— no element calls another directly. That is what buys the ordering rule
(projections fold and the read model commits BEFORE policies dispatch) and the
event store, and it is what lets the same wiring run under either scheduling
shell. A policy's command does not recurse into a procedure; it lands on the
command queue and becomes the next unit of work.

A :class:`Kitchen` is a bundle of ready-to-call command runners and query
callables bound to one ``SqlaAdapter`` (i.e. one database session), plus the
engine underneath. Build a fresh one per unit of work (per CLI run, per HTTP
request). Calling a command runner runs it *to quiescence*: the command, then
every event it causes, then every command those events dispatch — which is the
single-threaded phased-queue semantics, with this file playing the part the
scheduling shell plays in a deployed host.

The optional ``observer`` is called with ``(event_name, event)`` for every event
the feature emits — hosts use it to log, print, or collect the cascade.
"""

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from dizzy.engine import Engine, EventStore, FeatGraph

from gen_def.pydantic.commands import (
    RegisterIngredient,
    RegisterTool,
    DefineRecipe,
    AddRecipeStep,
    AddStepInput,
    StartBatch,
    AdvanceBatch,
)
from gen_def.pydantic.events import (
    IngredientRegistered,
    ToolRegistered,
    RecipeDefined,
    RecipeStepAdded,
    StepInputAdded,
    BatchOpened,
    StepPerformed,
    EntityConsumed,
    EntityProduced,
    EntityDerived,
    BatchCompleted,
    BatchRunFailed,
)
from gen_def.pydantic.query.get_recipe import GetRecipeInput, GetRecipeOutput
from gen_def.pydantic.query.get_recipe_steps import GetRecipeStepsInput, GetRecipeStepsOutput
from gen_def.pydantic.query.get_step_inputs import GetStepInputsInput, GetStepInputsOutput
from gen_def.pydantic.query.get_batch import GetBatchInput, GetBatchOutput
from gen_def.pydantic.query.check_inventory import CheckInventoryInput, CheckInventoryOutput
from gen_def.pydantic.query.find_blocked_batches import (
    FindBlockedBatchesInput,
    FindBlockedBatchesOutput,
)
from gen_def.pydantic.query.trace_provenance import TraceProvenanceInput, TraceProvenanceOutput
from gen_def.pydantic.query.list_recipes import ListRecipesInput, ListRecipesOutput
from gen_def.pydantic.query.list_batches import ListBatchesInput, ListBatchesOutput

from gen_int.python.adapters.sqla import SqlaAdapter

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
from gen_int.python.projection.ingredient_catalog_projection import ingredient_catalog_context
from gen_int.python.projection.tool_catalog_projection import tool_catalog_context
from gen_int.python.projection.recipe_catalog_projection import recipe_catalog_context
from gen_int.python.projection.step_catalog_projection import step_catalog_context
from gen_int.python.projection.step_input_catalog_projection import step_input_catalog_context
from gen_int.python.projection.batch_store_projection import batch_store_context
from gen_int.python.projection.batch_finalizer_projection import batch_finalizer_context
from gen_int.python.projection.batch_reblocker_projection import batch_reblocker_context
from gen_int.python.projection.inventory_store_projection import inventory_store_context
from gen_int.python.projection.inventory_consumer_projection import inventory_consumer_context
from gen_int.python.projection.generation_graph_projection import generation_graph_context
from gen_int.python.projection.derivation_graph_projection import derivation_graph_context
from gen_int.python.query.get_recipe import get_recipe_context
from gen_int.python.query.get_recipe_steps import get_recipe_steps_context
from gen_int.python.query.get_step_inputs import get_step_inputs_context
from gen_int.python.query.get_batch import get_batch_context
from gen_int.python.query.check_inventory import check_inventory_context
from gen_int.python.query.find_blocked_batches import find_blocked_batches_context
from gen_int.python.query.trace_provenance import trace_provenance_context
from gen_int.python.query.list_recipes import list_recipes_context
from gen_int.python.query.list_batches import list_batches_context

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


EventObserver = Callable[[str, Any], None]

FEAT_PATH = Path(__file__).parent / "recipes.feat.yaml"
"""This host names its own feat rather than letting the engine discover one.

``FeatGraph`` otherwise walks up from the cwd, which finds whichever feat file
is nearest — the wrong one, if this example is run from inside a larger repo.
A deployed host has the same problem and the same fix.
"""


def feat_graph() -> FeatGraph:
    """The recipes topology, with every declared name resolved to its class."""
    return FeatGraph.load(FEAT_PATH)


class CommandQueue:
    """The external command queue, in-process.

    A deployed host swaps this for a real one — ``dizzy.engine.st``'s durable
    sqlite queue, or ``mp``'s broker — without the wiring below changing at
    all, because the engine only ever calls ``put``.
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

    # The engine underneath, for hosts that want the store or the topology.
    engine: Engine

    # Commands (each runs a procedure; effects flow through events to projections).
    register_ingredient: Callable[[RegisterIngredient], None]
    register_tool: Callable[[RegisterTool], None]
    define_recipe: Callable[[DefineRecipe], None]
    add_recipe_step: Callable[[AddRecipeStep], None]
    add_step_input: Callable[[AddStepInput], None]
    start_batch: Callable[[StartBatch], None]
    advance_batch: Callable[[AdvanceBatch], None]

    # Queries (read the projected models back out).
    get_recipe: Callable[[GetRecipeInput], GetRecipeOutput]
    get_recipe_steps: Callable[[GetRecipeStepsInput], GetRecipeStepsOutput]
    get_step_inputs: Callable[[GetStepInputsInput], GetStepInputsOutput]
    get_batch: Callable[[GetBatchInput], GetBatchOutput]
    check_inventory: Callable[[CheckInventoryInput], CheckInventoryOutput]
    find_blocked_batches: Callable[[FindBlockedBatchesInput], FindBlockedBatchesOutput]
    trace_provenance: Callable[[TraceProvenanceInput], TraceProvenanceOutput]
    list_recipes: Callable[[ListRecipesInput], ListRecipesOutput]
    list_batches: Callable[[ListBatchesInput], ListBatchesOutput]


def build_kitchen(
    adapter: SqlaAdapter,
    observer: Optional[EventObserver] = None,
    store: Optional[EventStore] = None,
    command_queue: Optional[CommandQueue] = None,
    commit: Optional[Callable[[], None]] = None,
) -> Kitchen:
    """Wire the feature over one read/write adapter and return a :class:`Kitchen`.

    *store* defaults to an in-memory event stream, so an example or a test gets
    the real append-fold-dispatch path without leaving a file behind; a
    deployed host passes one backed by a path. *commit* defaults to committing
    the adapter's session once per event — the boundary the engine owns, which
    is what makes fold-then-dispatch true for the next reader.
    """
    store = store if store is not None else EventStore(path=":memory:", graph=feat_graph())
    queue = command_queue if command_queue is not None else CommandQueue()
    engine = Engine(
        command_queue=queue,
        store=store,
        observer=observer,
        commit=commit if commit is not None else adapter.session.commit,
    )

    # --- Queries, each bound to the read adapter ---
    def q_get_recipe(inp: GetRecipeInput) -> GetRecipeOutput:
        return get_recipe(inp, get_recipe_context(adapter=adapter))

    def q_get_recipe_steps(inp: GetRecipeStepsInput) -> GetRecipeStepsOutput:
        return get_recipe_steps(inp, get_recipe_steps_context(adapter=adapter))

    def q_get_step_inputs(inp: GetStepInputsInput) -> GetStepInputsOutput:
        return get_step_inputs(inp, get_step_inputs_context(adapter=adapter))

    def q_get_batch(inp: GetBatchInput) -> GetBatchOutput:
        return get_batch(inp, get_batch_context(adapter=adapter))

    def q_check_inventory(inp: CheckInventoryInput) -> CheckInventoryOutput:
        return check_inventory(inp, check_inventory_context(adapter=adapter))

    def q_find_blocked_batches(inp: FindBlockedBatchesInput) -> FindBlockedBatchesOutput:
        return find_blocked_batches(inp, find_blocked_batches_context(adapter=adapter))

    def q_trace_provenance(inp: TraceProvenanceInput) -> TraceProvenanceOutput:
        return trace_provenance(inp, trace_provenance_context(adapter=adapter))

    def q_list_recipes(inp: ListRecipesInput) -> ListRecipesOutput:
        return list_recipes(inp, list_recipes_context(adapter=adapter))

    def q_list_batches(inp: ListBatchesInput) -> ListBatchesOutput:
        return list_batches(inp, list_batches_context(adapter=adapter))

    # --- Projections: the data loop. One registration per event: projection
    # edge the feat declares. The engine calls these with (event, ingested_at)
    # after appending the event and before any policy sees it.
    for event_type, projection, context, name in (
        (IngredientRegistered, ingredient_catalog, ingredient_catalog_context,
         "ingredient_catalog"),
        (ToolRegistered, tool_catalog, tool_catalog_context, "tool_catalog"),
        (RecipeDefined, recipe_catalog, recipe_catalog_context, "recipe_catalog"),
        (RecipeStepAdded, step_catalog, step_catalog_context, "step_catalog"),
        (StepInputAdded, step_input_catalog, step_input_catalog_context,
         "step_input_catalog"),
        (BatchOpened, batch_store, batch_store_context, "batch_store"),
        (BatchCompleted, batch_finalizer, batch_finalizer_context, "batch_finalizer"),
        (BatchRunFailed, batch_reblocker, batch_reblocker_context, "batch_reblocker"),
        (EntityConsumed, inventory_consumer, inventory_consumer_context,
         "inventory_consumer"),
        (EntityProduced, inventory_store, inventory_store_context, "inventory_store"),
        (EntityProduced, generation_graph, generation_graph_context, "generation_graph"),
        (EntityDerived, derivation_graph, derivation_graph_context, "derivation_graph"),
    ):
        engine.register_projection(
            event_type,
            lambda event, ingested_at, p=projection, c=context: p(event, c(adapter=adapter)),
            name=name,
        )

    # step_performed is declared but folds nowhere: it is a fact for the stream
    # and for observers, not for a read model. The engine appends it regardless.

    # --- Policy: the reactivity loop. Its dispatch goes to the engine, so the
    # command lands on the queue instead of recursing into run_batch.
    engine.register_policy(
        EntityProduced,
        lambda event: advance_ready_batches(
            event,
            advance_ready_batches_context(
                emit=advance_ready_batches_emitters(
                    advance_batch=engine.dispatch_command,
                ),
                query=advance_ready_batches_queries(
                    find_blocked_batches=q_find_blocked_batches,
                ),
            ),
        ),
        name="advance_ready_batches",
    )

    # --- Procedures. Every emitter is engine.emit_event: a procedure's output
    # is a fact for the stream, and the engine decides what happens next.
    engine.register_procedure(
        RegisterIngredient,
        lambda c: record_ingredient(
            record_ingredient_context(
                emit=record_ingredient_emitters(ingredient_registered=engine.emit_event)
            ),
            c,
        ),
        name="record_ingredient",
    )
    engine.register_procedure(
        RegisterTool,
        lambda c: record_tool(
            record_tool_context(emit=record_tool_emitters(tool_registered=engine.emit_event)),
            c,
        ),
        name="record_tool",
    )
    engine.register_procedure(
        DefineRecipe,
        lambda c: record_recipe(
            record_recipe_context(emit=record_recipe_emitters(recipe_defined=engine.emit_event)),
            c,
        ),
        name="record_recipe",
    )
    engine.register_procedure(
        AddRecipeStep,
        lambda c: record_step(
            record_step_context(emit=record_step_emitters(recipe_step_added=engine.emit_event)),
            c,
        ),
        name="record_step",
    )
    engine.register_procedure(
        AddStepInput,
        lambda c: record_step_input(
            record_step_input_context(
                emit=record_step_input_emitters(step_input_added=engine.emit_event)
            ),
            c,
        ),
        name="record_step_input",
    )
    engine.register_procedure(
        StartBatch,
        lambda c: open_batch(
            open_batch_context(
                emit=open_batch_emitters(batch_opened=engine.emit_event),
                query=open_batch_queries(
                    get_recipe=q_get_recipe, check_inventory=q_check_inventory
                ),
            ),
            c,
        ),
        name="open_batch",
    )
    engine.register_procedure(
        AdvanceBatch,
        lambda c: run_batch(
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
                    get_batch=q_get_batch,
                    get_recipe=q_get_recipe,
                    get_recipe_steps=q_get_recipe_steps,
                    get_step_inputs=q_get_step_inputs,
                    check_inventory=q_check_inventory,
                ),
            ),
            c,
        ),
        name="run_batch",
    )

    def run_to_quiescence(command: Any) -> None:
        """Run one command, then every command its events dispatched.

        The engine drains the EVENT queue itself; draining the COMMAND queue is
        the scheduling shell's job, and in a single-process host like this one
        that job is a while-loop. Under ``dizzy.engine.st`` it is a worker
        claiming rows; under ``mp`` it is N processes. Same wiring either way.
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
        get_recipe=q_get_recipe,
        get_recipe_steps=q_get_recipe_steps,
        get_step_inputs=q_get_step_inputs,
        get_batch=q_get_batch,
        check_inventory=q_check_inventory,
        find_blocked_batches=q_find_blocked_batches,
        trace_provenance=q_trace_provenance,
        list_recipes=q_list_recipes,
        list_batches=q_list_batches,
    )
