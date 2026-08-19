# Persist each registered ingredient into the catalog.
from gen_int.python.projection.ingredient_catalog_projection import ingredient_catalog_context
from gen_def.pydantic.events import IngredientRegistered
from gen_def.sqla.models.catalog import Ingredient


def ingredient_catalog(
    event: IngredientRegistered,
    context: ingredient_catalog_context,
) -> None:
    context.adapter.session.merge(
        Ingredient(
            ingredient_type=event.ingredient_type,
            name=event.name,
            unit=event.unit,
        )
    )
