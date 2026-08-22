# Insert each opened batch with its status and requires_type.
from gen_int.python.projection.batch_store_projection import batch_store_context
from gen_def.pydantic.events import BatchOpened
from gen_def.sqla.models.batches import Batch


def batch_store(
    event: BatchOpened,
    context: batch_store_context,
) -> None:
    context.adapter.session.merge(
        Batch(
            id=event.batch_id,
            recipe_id=event.recipe_id,
            requires_type=event.requires_type,
            status=event.status,
            opened_at=event.opened_at,
        )
    )
