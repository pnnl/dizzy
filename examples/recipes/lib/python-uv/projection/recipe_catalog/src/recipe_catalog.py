# Persist each defined recipe header into the catalog.
from gen_int.python.projection.recipe_catalog_projection import recipe_catalog_context
from gen_def.pydantic.events import RecipeDefined
from gen_def.sqla.models.catalog import Recipe


def recipe_catalog(
    event: RecipeDefined,
    context: recipe_catalog_context,
) -> None:
    context.adapter.session.merge(
        Recipe(
            recipe_id=event.recipe_id,
            name=event.name,
            requires_type=event.requires_type,
            output_type=event.output_type,
            output_unit=event.output_unit,
        )
    )
