# Persist each registered tool into the catalog.
from gen_int.python.projection.tool_catalog_projection import tool_catalog_context
from gen_def.pydantic.events import ToolRegistered
from gen_def.sqla.models.catalog import Tool


def tool_catalog(
    event: ToolRegistered,
    context: tool_catalog_context,
) -> None:
    context.adapter.session.merge(
        Tool(tool_id=event.tool_id, name=event.name)
    )
