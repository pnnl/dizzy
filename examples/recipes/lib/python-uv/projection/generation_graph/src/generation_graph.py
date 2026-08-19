# Record each entity's generation (prov:wasGeneratedBy) into the provenance graph.
from gen_int.python.projection.generation_graph_projection import generation_graph_context
from gen_def.pydantic.events import EntityProduced
from gen_def.sqla.models.provenance import ProvGeneration


def generation_graph(
    event: EntityProduced,
    context: generation_graph_context,
) -> None:
    context.adapter.session.merge(
        ProvGeneration(
            entity_id=event.entity_id,
            entity_type=event.entity_type,
            batch_id=event.batch_id,
            activity_id=event.activity_id,
            produced_at=event.produced_at,
        )
    )
