from __future__ import annotations

import re
import sys
from datetime import (
    date,
    datetime,
    time
)
from decimal import Decimal
from enum import Enum
from typing import (
    Any,
    ClassVar,
    Literal,
    Optional,
    Union
)

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SerializationInfo,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer
)


metamodel_version = "1.11.0"
version = "None"


class ConfiguredBaseModel(BaseModel):
    model_config = ConfigDict(
        serialize_by_alias = True,
        validate_by_name = True,
        validate_assignment = True,
        validate_default = True,
        extra = "forbid",
        arbitrary_types_allowed = True,
        use_enum_values = True,
        strict = False,
    )





class LinkMLMeta(RootModel):
    root: dict[str, Any] = {}
    model_config = ConfigDict(frozen=True)

    def __getattr__(self, key:str):
        return getattr(self.root, key)

    def __getitem__(self, key:str):
        return self.root[key]

    def __setitem__(self, key:str, value):
        self.root[key] = value

    def __contains__(self, key:str) -> bool:
        return key in self.root


linkml_meta = LinkMLMeta({'default_prefix': 'dizzy',
     'default_range': 'string',
     'description': 'LinkML meta-schema defining the structure of libconfig.yaml '
                    'files used to configure which dizzy elements are implemented '
                    'in which language runtimes.',
     'id': 'https://example.org/dizzy/libconfig',
     'imports': ['linkml:types'],
     'name': 'dizzy-libconfig-schema',
     'prefixes': {'dizzy': {'prefix_prefix': 'dizzy',
                            'prefix_reference': 'https://example.org/dizzy/'},
                  'linkml': {'prefix_prefix': 'linkml',
                             'prefix_reference': 'https://w3id.org/linkml/'}},
     'source_file': 'dizzy/src/dizzy/def/libconfig.yaml',
     'title': 'Dizzy Library Configuration Schema'} )

class LanguageRuntime(str, Enum):
    """
    Supported language runtime patterns
    """
    python_uv = "python-uv"
    """
    Python with uv and pyproject.toml
    """
    rust_cargo = "rust-cargo"
    """
    Rust with Cargo.toml
    """
    typescript_npm = "typescript-npm"
    """
    TypeScript with package.json and tsconfig.json
    """


class ContractKind(str, Enum):
    """
    Contract families that can be emitted as JSON Schema
    """
    commands = "commands"
    """
    def/commands.yaml — the write intentions
    """
    events = "events"
    """
    def/events.yaml — the recorded facts
    """
    queries = "queries"
    """
    def/queries/*.yaml — the read surface (input + output shapes)
    """
    models = "models"
    """
    def/models/*.yaml — the projected read models
    """



class LibConfig(ConfiguredBaseModel):
    """
    Top-level container for a Dizzy library configuration file.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/libconfig'})

    procedures: Optional[list[ElementBinding]] = Field(default=None, description="""Procedure bindings to language runtimes""", json_schema_extra = { "linkml_meta": {'domain_of': ['LibConfig']} })
    policies: Optional[list[ElementBinding]] = Field(default=None, description="""Policy bindings to language runtimes""", json_schema_extra = { "linkml_meta": {'domain_of': ['LibConfig']} })
    queries: Optional[list[ElementBinding]] = Field(default=None, description="""Query bindings to language runtimes""", json_schema_extra = { "linkml_meta": {'domain_of': ['LibConfig']} })
    projections: Optional[list[ElementBinding]] = Field(default=None, description="""Projection bindings to language runtimes""", json_schema_extra = { "linkml_meta": {'domain_of': ['LibConfig']} })
    json_schema: Optional[JsonSchemaConfig] = Field(default=None, description="""Runtime-neutral JSON Schema emission. Omit the section entirely to emit nothing — that is the backwards-compatible default for libconfig.yaml files written before this section existed.""", json_schema_extra = { "linkml_meta": {'domain_of': ['LibConfig']} })


class JsonSchemaConfig(ConfiguredBaseModel):
    """
    Controls the JSON Schema contracts emitted by `dizzy generate static`.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/libconfig'})

    contracts: Optional[list[ContractKind]] = Field(default=None, description="""Which contract kinds get a JSON Schema. Defaults to [commands, queries] when the json_schema section is present but the key is omitted.""", json_schema_extra = { "linkml_meta": {'domain_of': ['JsonSchemaConfig']} })
    output_dir: Optional[str] = Field(default=None, description="""Directory for the emitted schemas, relative to the generate output directory. Defaults to `gen_schema`.""", json_schema_extra = { "linkml_meta": {'domain_of': ['JsonSchemaConfig']} })


class ElementBinding(ConfiguredBaseModel):
    """
    Binds a named element to one or more language runtimes.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/libconfig'})

    name: str = Field(default=..., description="""Element name matching a name in the corresponding feat section""", json_schema_extra = { "linkml_meta": {'domain_of': ['ElementBinding']} })
    runtimes: Optional[list[LanguageRuntime]] = Field(default=None, description="""Language runtimes that implement this element""", json_schema_extra = { "linkml_meta": {'domain_of': ['ElementBinding']} })


# Model rebuild
# see https://pydantic-docs.helpmanual.io/usage/models/#rebuilding-a-model
LibConfig.model_rebuild()
JsonSchemaConfig.model_rebuild()
ElementBinding.model_rebuild()
