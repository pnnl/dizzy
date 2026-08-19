"""Run the chained recipe cascade as a CLI host.

This is one host for the recipes feature; `server.py` is another. Both build a
:class:`~kitchen.Kitchen` (see `kitchen.py`) — the shared host wiring that owns
event routing and the policy cascade. This file just owns an in-memory database,
prints what happens, and drives a scenario.

The point of the example is a POLICY-driven cascade over PROV-style events. Three
recipes chain — each one's output entity is the next one's required input:

    active_starter ─▶ sourdough_loaf ─▶ garlic_croutons

All three batches are opened up front. The loaf and croutons batches open
*blocked*, because the entity they require does not exist yet. We then advance
only the starter batch by hand. Each batch ends by emitting entity_produced
(prov:wasGeneratedBy); the Kitchen routes that to the projections (data loop) and
then to the advance_ready_batches policy (reactivity loop), which dispatches
advance_batch for whichever batch was waiting. So finishing the starter cascades
through the loaf and into the croutons from a single trigger.

Run inside the workspace environment, layering in the DIZZY runtime kit so the
generated workspace stays untouched (from the repo root):

    uv sync --project examples/recipes/lib/python-uv
    uv run --project examples/recipes/lib/python-uv --with-editable . \
        python examples/recipes/demo.py

``kitchen.py`` runs the feature on ``dizzy.engine``, so the host needs DIZZY
itself installed alongside the generated packages. Outside this repository that
is ``--with dizzy``.
"""

from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from gen_def.pydantic.commands import (
    RegisterIngredient,
    RegisterTool,
    DefineRecipe,
    AddRecipeStep,
    AddStepInput,
    StartBatch,
    AdvanceBatch,
)
from gen_def.pydantic.query.get_batch import GetBatchInput
from gen_def.pydantic.query.trace_provenance import TraceProvenanceInput

# Each model schema declares its own SQLAlchemy Base; create them all.
from gen_def.sqla.models.catalog import Base as CatalogBase
from gen_def.sqla.models.batches import Base as BatchesBase
from gen_def.sqla.models.inventory import Base as InventoryBase
from gen_def.sqla.models.provenance import Base as ProvenanceBase

from gen_int.python.adapters.sqla import SqlaAdapter

from kitchen import build_kitchen


def narrate(name: str, event: Any) -> None:
    """Print the events worth showing as the scenario runs."""
    if name == "batch_opened":
        flag = "" if event.status == "ready" else f" (blocked on {event.requires_type})"
        print(f"  opened batch {event.batch_id} [{event.status}]{flag}")
    elif name == "step_performed":
        tool = f" with {event.tool_id}" if event.tool_id else ""
        print(f"    · {event.batch_id} step {event.step_order}: {event.activity_kind}{tool}")
    elif name == "entity_produced":
        print(f"  ✓ {event.batch_id} produced {event.entity_type} ({event.entity_id})")


def main() -> None:
    # The host owns persistence. Here: an in-memory SQLite database.
    engine = create_engine("sqlite://")
    for base in (CatalogBase, BatchesBase, InventoryBase, ProvenanceBase):
        base.metadata.create_all(engine)
    session = Session(engine)
    kitchen = build_kitchen(SqlaAdapter(session=session), observer=narrate)

    # --- Build the catalog: ingredients, tools, recipes, and typed steps ---
    print("Catalog:")
    for ing, name, unit in [
        ("flour", "Bread flour", "g"),
        ("water", "Water", "ml"),
        ("salt", "Fine sea salt", "g"),
        ("olive_oil", "Olive oil", "ml"),
        ("garlic", "Garlic", "clove"),
    ]:
        kitchen.register_ingredient(RegisterIngredient(ingredient_type=ing, name=name, unit=unit))
    for tool_id, name in [
        ("jar", "Glass fermentation jar"),
        ("mixer", "Stand mixer"),
        ("oven", "Deck oven"),
        ("knife", "Chef's knife"),
        ("bowl", "Mixing bowl"),
    ]:
        kitchen.register_tool(RegisterTool(tool_id=tool_id, name=name))
    print("  5 ingredients, 5 tools registered")

    def define(recipe_id, name, requires_type, output_type, output_unit, steps, inputs):
        kitchen.define_recipe(
            DefineRecipe(
                recipe_id=recipe_id,
                name=name,
                requires_type=requires_type,
                output_type=output_type,
                output_unit=output_unit,
            )
        )
        for order, kind, tool in steps:
            kitchen.add_recipe_step(
                AddRecipeStep(
                    recipe_id=recipe_id, step_order=order, activity_kind=kind, tool_id=tool
                )
            )
        for order, ingredient, qty, unit in inputs:
            kitchen.add_step_input(
                AddStepInput(
                    recipe_id=recipe_id,
                    step_order=order,
                    ingredient_type=ingredient,
                    qty=qty,
                    unit=unit,
                )
            )

    define(
        "cultivate_starter",
        "Sourdough starter",
        None,
        "active_starter",
        "jar",
        steps=[(1, "mix", "jar"), (2, "ferment", "jar")],
        inputs=[(1, "flour", 100, "g"), (1, "water", 100, "ml")],
    )
    define(
        "bake_sourdough",
        "Sourdough loaf",
        "active_starter",
        "sourdough_loaf",
        "loaf",
        steps=[(1, "mix", "mixer"), (2, "ferment", "jar"), (3, "bake", "oven")],
        inputs=[(1, "flour", 500, "g"), (1, "water", 350, "ml"), (1, "salt", 10, "g")],
    )
    define(
        "make_croutons",
        "Garlic croutons",
        "sourdough_loaf",
        "garlic_croutons",
        "bowl",
        steps=[(1, "cut", "knife"), (2, "toss", "bowl"), (3, "bake", "oven")],
        inputs=[(2, "olive_oil", 30, "ml"), (2, "garlic", 2, "clove")],
    )
    print("  3 recipes defined (starter ▶ loaf ▶ croutons)")

    # --- Open all three batches up front ---
    # The loaf and croutons batches require entities that do not exist yet, so they
    # open *blocked*. The starter batch requires nothing, so it opens ready.
    print("\nOpening batches:")
    kitchen.start_batch(StartBatch(batch_id="croutons-1", recipe_id="make_croutons"))
    kitchen.start_batch(StartBatch(batch_id="loaf-1", recipe_id="bake_sourdough"))
    kitchen.start_batch(StartBatch(batch_id="starter-1", recipe_id="cultivate_starter"))

    # --- One trigger: advance the starter. The policy cascades the rest. ---
    print("\nAdvancing starter-1 (watch the cascade):")
    kitchen.advance_batch(AdvanceBatch(batch_id="starter-1"))

    # --- Read the results back out ---
    print("\nFinal batch states:")
    for batch_id in ("starter-1", "loaf-1", "croutons-1"):
        b = kitchen.get_batch(GetBatchInput(batch_id=batch_id))
        print(f"  {batch_id}: {b.status}")

    print("\nProvenance of the croutons (trace_provenance):")
    trace = kitchen.trace_provenance(
        TraceProvenanceInput(entity_id="croutons-1:garlic_croutons")
    )
    for line in trace.lines:
        print(f"  {line}")


if __name__ == "__main__":
    main()
