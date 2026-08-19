"""An HTTP host for the recipes feature — a thin FastAPI layer over the Kitchen.

This is a sibling of ``demo.py``: where the demo is a CLI host, this is an HTTP
host. Both build a :class:`~kitchen.Kitchen` (the shared wiring in ``kitchen.py``)
and drive the feature through it. The server's only jobs are to own a durable
database and to map HTTP requests onto the Kitchen's commands and queries.

The generated Pydantic command/query models *are* the API contract, so they are
used directly as request bodies and response models — open ``/docs`` to see the
whole schema. A command runs a procedure and returns the events it caused
(including any policy cascade); a query reads a projected model back out.

State lives in a SQLite file (``recipes.db`` by default, override with
``RECIPES_DB``) so it survives restarts — the event-sourced read models are
rebuilt into it and queried back out.

Run inside the workspace environment, layering in the web deps (from the repo root):

    uv run --project examples/recipes/lib/python-uv \\
        --with fastapi --with "uvicorn[standard]" \\
        python examples/recipes/server.py

Then open http://127.0.0.1:8000/ for the browser UI, or /docs for the raw API.
"""

import os
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.staticfiles import StaticFiles
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from gen_def.pydantic.commands import (
    RegisterIngredient,
    RegisterTool,
    DefineRecipe,
    AddRecipeStep,
    AddStepInput,
    StartBatch,
    AdvanceBatch,
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

from gen_def.sqla.models.catalog import Base as CatalogBase
from gen_def.sqla.models.batches import Base as BatchesBase
from gen_def.sqla.models.inventory import Base as InventoryBase
from gen_def.sqla.models.provenance import Base as ProvenanceBase

from gen_int.python.adapters.sqla import SqlaAdapter

from kitchen import Kitchen, build_kitchen


# --- The host owns persistence: a durable SQLite file shared across requests ---
DB_PATH = os.environ.get("RECIPES_DB", str(Path(__file__).parent / "recipes.db"))
engine = create_engine(f"sqlite:///{DB_PATH}")
for base in (CatalogBase, BatchesBase, InventoryBase, ProvenanceBase):
    base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

app = FastAPI(
    title="Recipe kitchen",
    description=(
        "Send commands (writes) and run queries (reads) against the chained, "
        "event-sourced recipe feature. A command returns the PROV-style events it "
        "caused, including any policy cascade."
    ),
)


def run_command(apply: Callable[[Kitchen], None]) -> dict[str, Any]:
    """Run one command in a fresh session, returning the events it caused."""
    events: list[dict[str, Any]] = []
    session: Session = SessionLocal()
    try:
        kitchen = build_kitchen(
            SqlaAdapter(session=session),
            observer=lambda name, event: events.append(
                {"event": name, "data": jsonable_encoder(event)}
            ),
        )
        try:
            apply(kitchen)
        except ValueError as exc:
            # Domain validation failures from a procedure become 400s.
            raise HTTPException(status_code=400, detail=str(exc))
    finally:
        session.close()
    return {"events": events}


def run_query(apply: Callable[[Kitchen], Any]) -> Any:
    """Run one query in a fresh read session."""
    session: Session = SessionLocal()
    try:
        return apply(build_kitchen(SqlaAdapter(session=session)))
    finally:
        session.close()


# --- Commands (writes). Each takes its generated command model as the body. ---
@app.post("/ingredients", tags=["commands"])
def register_ingredient(command: RegisterIngredient) -> dict[str, Any]:
    return run_command(lambda k: k.register_ingredient(command))


@app.post("/tools", tags=["commands"])
def register_tool(command: RegisterTool) -> dict[str, Any]:
    return run_command(lambda k: k.register_tool(command))


@app.post("/recipes", tags=["commands"])
def define_recipe(command: DefineRecipe) -> dict[str, Any]:
    return run_command(lambda k: k.define_recipe(command))


@app.post("/recipe-steps", tags=["commands"])
def add_recipe_step(command: AddRecipeStep) -> dict[str, Any]:
    return run_command(lambda k: k.add_recipe_step(command))


@app.post("/step-inputs", tags=["commands"])
def add_step_input(command: AddStepInput) -> dict[str, Any]:
    return run_command(lambda k: k.add_step_input(command))


@app.post("/batches", tags=["commands"])
def start_batch(command: StartBatch) -> dict[str, Any]:
    return run_command(lambda k: k.start_batch(command))


@app.post("/batches/advance", tags=["commands"])
def advance_batch(command: AdvanceBatch) -> dict[str, Any]:
    """Advance a batch. Returns every event in the resulting cascade."""
    return run_command(lambda k: k.advance_batch(command))


# --- Queries (reads). ---
@app.get("/recipes/{recipe_id}", response_model=GetRecipeOutput, tags=["queries"])
def get_recipe(recipe_id: str) -> GetRecipeOutput:
    result = run_query(lambda k: k.get_recipe(GetRecipeInput(recipe_id=recipe_id)))
    if result.recipe_id is None:
        raise HTTPException(status_code=404, detail=f"unknown recipe: {recipe_id}")
    return result


@app.get("/recipes/{recipe_id}/steps", response_model=GetRecipeStepsOutput, tags=["queries"])
def get_recipe_steps(recipe_id: str) -> GetRecipeStepsOutput:
    return run_query(lambda k: k.get_recipe_steps(GetRecipeStepsInput(recipe_id=recipe_id)))


@app.get("/recipes/{recipe_id}/inputs", response_model=GetStepInputsOutput, tags=["queries"])
def get_step_inputs(recipe_id: str) -> GetStepInputsOutput:
    return run_query(lambda k: k.get_step_inputs(GetStepInputsInput(recipe_id=recipe_id)))


@app.get("/batches/{batch_id}", response_model=GetBatchOutput, tags=["queries"])
def get_batch(batch_id: str) -> GetBatchOutput:
    result = run_query(lambda k: k.get_batch(GetBatchInput(batch_id=batch_id)))
    if result.recipe_id is None:
        raise HTTPException(status_code=404, detail=f"unknown batch: {batch_id}")
    return result


@app.get("/inventory/{entity_type}", response_model=CheckInventoryOutput, tags=["queries"])
def check_inventory(entity_type: str) -> CheckInventoryOutput:
    return run_query(lambda k: k.check_inventory(CheckInventoryInput(entity_type=entity_type)))


@app.get(
    "/blocked-batches/{entity_type}",
    response_model=FindBlockedBatchesOutput,
    tags=["queries"],
)
def find_blocked_batches(entity_type: str) -> FindBlockedBatchesOutput:
    return run_query(
        lambda k: k.find_blocked_batches(FindBlockedBatchesInput(entity_type=entity_type))
    )


@app.get("/provenance/{entity_id}", response_model=TraceProvenanceOutput, tags=["queries"])
def trace_provenance(entity_id: str) -> TraceProvenanceOutput:
    return run_query(lambda k: k.trace_provenance(TraceProvenanceInput(entity_id=entity_id)))


@app.get("/recipes", response_model=ListRecipesOutput, tags=["queries"])
def list_recipes() -> ListRecipesOutput:
    return run_query(lambda k: k.list_recipes(ListRecipesInput()))


@app.get("/batches", response_model=ListBatchesOutput, tags=["queries"])
def list_batches() -> ListBatchesOutput:
    return run_query(lambda k: k.list_batches(ListBatchesInput()))


# --- Host admin (a demo affordance, not a domain command) ---
@app.post("/reset", tags=["host"])
def reset() -> dict[str, str]:
    """Drop and recreate every model table, so the UI can restart cleanly."""
    for base in (CatalogBase, BatchesBase, InventoryBase, ProvenanceBase):
        base.metadata.drop_all(engine)
        base.metadata.create_all(engine)
    return {"status": "reset"}


# --- The browser UI, served from this same app at / (must be mounted last) ---
UI_DIR = Path(__file__).parent / "ui"
app.mount("/", StaticFiles(directory=str(UI_DIR), html=True), name="ui")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
