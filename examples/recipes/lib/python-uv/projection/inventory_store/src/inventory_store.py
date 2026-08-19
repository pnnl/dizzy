# Record each produced entity lot as available inventory.
from gen_int.python.projection.inventory_store_projection import inventory_store_context
from gen_def.pydantic.events import EntityProduced
from gen_def.sqla.models.inventory import InventoryEntity


def inventory_store(
    event: EntityProduced,
    context: inventory_store_context,
) -> None:
    context.adapter.session.merge(
        InventoryEntity(
            id=event.entity_id,
            entity_type=event.entity_type,
            qty=int(event.qty),
            unit=event.unit,
            batch_id=event.batch_id,
            available=True,
        )
    )
