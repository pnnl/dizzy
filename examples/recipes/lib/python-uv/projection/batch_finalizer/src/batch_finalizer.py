# Mark a batch completed when it finishes running.
from gen_int.python.projection.batch_finalizer_projection import batch_finalizer_context
from gen_def.pydantic.events import BatchCompleted
from gen_def.sqla.models.batches import Batch


def batch_finalizer(
    event: BatchCompleted,
    context: batch_finalizer_context,
) -> None:
    session = context.adapter.session
    batch = session.get(Batch, event.batch_id)
    if batch is not None:
        batch.status = "completed"
