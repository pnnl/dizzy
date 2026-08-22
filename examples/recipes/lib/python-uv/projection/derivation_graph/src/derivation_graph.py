# Record each output->source derivation edge (prov:wasDerivedFrom).
from gen_int.python.projection.derivation_graph_projection import derivation_graph_context
from gen_def.pydantic.events import EntityDerived
from gen_def.sqla.models.provenance import ProvDerivation


def derivation_graph(
    event: EntityDerived,
    context: derivation_graph_context,
) -> None:
    context.adapter.session.merge(
        ProvDerivation(
            output_entity_id=event.output_entity_id,
            source_entity_id=event.source_entity_id,
        )
    )
