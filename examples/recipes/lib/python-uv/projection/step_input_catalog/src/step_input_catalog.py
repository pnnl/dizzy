# Persist each step input into the catalog, keyed by recipe + order + ingredient.
from gen_int.python.projection.step_input_catalog_projection import step_input_catalog_context
from gen_def.pydantic.events import StepInputAdded
from gen_def.sqla.models.catalog import StepInput


def step_input_catalog(
    event: StepInputAdded,
    context: step_input_catalog_context,
) -> None:
    context.adapter.session.merge(
        StepInput(
            id=f"{event.recipe_id}#{event.step_order}#{event.ingredient_type}",
            recipe_id=event.recipe_id,
            step_order=event.step_order,
            ingredient_type=event.ingredient_type,
            qty=int(event.qty),
            unit=event.unit,
        )
    )
