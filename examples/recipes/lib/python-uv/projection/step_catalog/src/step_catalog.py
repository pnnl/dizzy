# Persist each recipe step into the catalog, keyed by recipe + order.
from gen_int.python.projection.step_catalog_projection import step_catalog_context
from gen_def.pydantic.events import RecipeStepAdded
from gen_def.sqla.models.catalog import RecipeStep


def step_catalog(
    event: RecipeStepAdded,
    context: step_catalog_context,
) -> None:
    context.adapter.session.merge(
        RecipeStep(
            id=f"{event.recipe_id}#{event.step_order}",
            recipe_id=event.recipe_id,
            step_order=event.step_order,
            activity_kind=event.activity_kind,
            tool_id=event.tool_id,
        )
    )
