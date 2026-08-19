# Mark an inventory lot unavailable once it is consumed. Pantry ingredients are not
# tracked as lots, so a miss is a no-op.
from gen_int.python.projection.inventory_consumer_projection import inventory_consumer_context
from gen_def.pydantic.events import EntityConsumed
from gen_def.sqla.models.inventory import InventoryEntity


def inventory_consumer(
    event: EntityConsumed,
    context: inventory_consumer_context,
) -> None:
    session = context.adapter.session
    lot = session.get(InventoryEntity, event.entity_id)
    if lot is not None:
        lot.available = False
