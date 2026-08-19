# Return a batch to blocked when a run attempt failed for lack of an input, so a
# later lot of that type can pull it again. The batch_run_failed event itself stays
# in the stream as the audit fact of the failed attempt.
from gen_int.python.projection.batch_reblocker_projection import batch_reblocker_context
from gen_def.pydantic.events import BatchRunFailed
from gen_def.sqla.models.batches import Batch


def batch_reblocker(
    event: BatchRunFailed,
    context: batch_reblocker_context,
) -> None:
    session = context.adapter.session
    batch = session.get(Batch, event.batch_id)
    if batch is not None:
        batch.status = "blocked"
