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
     'description': 'LinkML meta-schema defining the structure of topology.yaml '
                    "files. A topology says WHERE a feature's elements run — which "
                    'of them share a cursor over the event log, how many workers '
                    'serve each such group, and what carries the wake-up. It never '
                    'says WHAT they do; that is the feature-file, and the two are '
                    'read together by `dizzy generate wiring`.\n'
                    'The unit a topology names is the CONSUMER GROUP: a set of '
                    'elements sharing one cursor. Two workers on one group is a '
                    'scaling decision. Two groups is a SEMANTIC decision — they '
                    "advance independently and can observe each other's effects "
                    'out of order — which is why groups are declared here, '
                    'reviewed, and validated against the feature-file at '
                    'generation time rather than discovered under load.',
     'id': 'https://example.org/dizzy/topology',
     'imports': ['linkml:types'],
     'name': 'dizzy-topology-schema',
     'prefixes': {'dizzy': {'prefix_prefix': 'dizzy',
                            'prefix_reference': 'https://example.org/dizzy/'},
                  'linkml': {'prefix_prefix': 'linkml',
                             'prefix_reference': 'https://w3id.org/linkml/'}},
     'source_file': 'dizzy/src/dizzy/def/topology.yaml',
     'title': 'Dizzy Deployment Topology Schema'} )

class ElementKind(str, Enum):
    """
    The element kinds a topology may place. Queries are absent deliberately: they are reads over a model, so their placement is a question about the read substrate, not about the log.
    """
    procedures = "procedures"
    """
    Decide a command and record the outcome.
    """
    policies = "policies"
    """
    React to an event by dispatching a command.
    """
    projections = "projections"
    """
    Fold events into a model.
    """


class ExecutorKind(str, Enum):
    """
    Where the single-machine executor runs a group's elements. Both lanes are one machine and one log: the pool buys freedom from head-of-line blocking, never a second writer.
    """
    inline = "inline"
    """
    The main lane, in the host process. The default, the only lane that appends to the log, and the reason a topology-free build costs nothing.
    """
    pool = "pool"
    """
    A background lane served by a spawned process pool. A forty-second turn there stalls nothing, paid for with a process per worker and with the group being unable to hold a projection or any element that reads a model.
    """


class TransportKind(str, Enum):
    """
    Transports that can carry the doorbell.
    """
    inprocess = "inprocess"
    """
    In-process notification. The default, and the reason a single-group topology costs a consumer nothing — no broker, no daemon, no IPC.
    """
    zmq_ipc = "zmq-ipc"
    """
    ZeroMQ over an ipc:// socket — brokerless, same host.
    """
    nats = "nats"
    """
    NATS subject.
    """
    postgres_notify = "postgres-notify"
    """
    Postgres LISTEN/NOTIFY, for a deployment that already runs one.
    """
    redis = "redis"
    """
    Redis pub/sub.
    """


class CursorStoreKind(str, Enum):
    """
    Stores that can hold group cursors.
    """
    memory = "memory"
    """
    In-memory. Every boot replays from zero, which is correct but unbounded in cost as the log grows. Appropriate for tests and single-shot runs.
    """
    sqlite = "sqlite"
    """
    A SQLite file beside the event store.
    """
    postgres = "postgres"
    """
    A Postgres table, for a multi-host deployment.
    """


class BackoffKind(str, Enum):
    """
    How retry delay grows.
    """
    none = "none"
    """
    Retry immediately.
    """
    fixed = "fixed"
    """
    Wait `initial_delay_ms` before every attempt.
    """
    exponential = "exponential"
    """
    Double the delay each attempt, starting at `initial_delay_ms`.
    """



class Topology(ConfiguredBaseModel):
    """
    Top-level container for a deployment topology. Omitting the file entirely is legal and means the default topology: one group named `default` holding every element, in-process transport, in-memory cursors — behaviourally identical to a build with no topology support at all.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/topology'})

    description: Optional[str] = Field(default=None, description="""What this topology is for. Free prose; it is a reviewed artifact.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Topology', 'ConsumerGroup']} })
    transport: Optional[Transport] = Field(default=None, description="""Default doorbell for every group. A group may override it. The transport carries \"wake up, there is work\" and nothing else — correctness comes from the cursor, so a dropped notification costs latency and never costs work.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Topology', 'ConsumerGroup']} })
    cursors: Optional[CursorStore] = Field(default=None, description="""Where group cursors are persisted. Durable so a boot is snapshot+tail rather than replay-from-zero, but disposable: losing one must be safe, not merely recoverable.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Topology']} })
    defaults: Optional[GroupDefaults] = Field(default=None, description="""Settings every group inherits unless it states otherwise.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Topology']} })
    priority: Optional[CommandPriority] = Field(default=None, description="""Which command types jump the queue. Omitting it means every command is batch, which is the order a queue with no priority already had.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Topology']} })
    groups: Optional[list[ConsumerGroup]] = Field(default=None, description="""The declared consumer groups. Every element the feature-file declares must be claimed by exactly one group; generation fails if an element is unclaimed (and no group is `catch_all`) or claimed twice.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Topology']} })


class ConsumerGroup(ConfiguredBaseModel):
    """
    A set of elements sharing ONE cursor over the event log. Not \"a process\" — how many processes serve it is `workers`.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/topology'})

    name: str = Field(default=..., description="""Identifier for this group. Appears in generated wiring package names and in operational surfaces, never in authored element code.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup']} })
    description: Optional[str] = Field(default=None, description="""Why this group exists, in deployment terms.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Topology', 'ConsumerGroup']} })
    include: Optional[list[Selector]] = Field(default=None, description="""Which elements this group claims. Selectors are additive, and two groups whose selectors match the same element is a generation error — placement must be decided here, not by declaration order. To avoid enumerating the tail, mark one group `catch_all` instead.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup']} })
    catch_all: Optional[bool] = Field(default=None, description="""This group claims every element no other group claimed. Exactly one group in a topology may set it, and that group's `include` selectors (if any) are additive with what it sweeps up. Without this, adding an element to the feature-file would silently leave it unplaced — or force every topology to be edited in lockstep with the design.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup']} })
    executor: Optional[ExecutorKind] = Field(default=None, description="""How the single-machine executor serves this group. `inline` runs it on the main lane, inside the host process — the lane that writes the log, and the only lane a default topology has. `pool` runs it on a background lane served by a spawned process pool, so a slow element cannot hold the head of the main lane's queue while it works. Defaults to `inline`.
This is the head-of-line knob, and it is a different problem from `priority`: priority decides who is claimed next, which cannot help at all while a forty-second turn is already running. Only moving that turn off the main lane can.
A background lane is concurrent with the main one by construction, so it asks nothing new about eligibility — rule 1 already refuses a group holding a projection any concurrency, and rule 3 refuses one holding any element that declares `queries:`. What an element reads is what decides whether it may leave the main lane, and there is deliberately no second answer to that question.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup']} })
    workers: Optional[int] = Field(default=None, description="""How many processes serve this group. A scaling decision, safe to change without re-reasoning about correctness. Defaults to 1.""", ge=1, json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup', 'GroupDefaults']} })
    max_in_flight: Optional[int] = Field(default=None, description="""Upper bound on units of work a single worker holds at once — the backpressure knob for fan-out workloads, where one command can produce thousands of downstream commands. Unset means unbounded.""", ge=1, json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup', 'GroupDefaults']} })
    retry: Optional[RetryPolicy] = Field(default=None, description="""What happens when an element raises.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup', 'GroupDefaults']} })
    transport: Optional[Transport] = Field(default=None, description="""Overrides the topology-level transport for this group alone.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Topology', 'ConsumerGroup']} })


class Selector(ConfiguredBaseModel):
    """
    Picks elements out of the feature-file by property rather than by enumeration — a feature with 124 elements should not need 124 lines here. Within one selector the stated fields are ANDed.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/topology'})

    kind: Optional[ElementKind] = Field(default=None, description="""Restrict to one element kind. Omit to match any kind a selector's other fields allow.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Selector', 'Transport', 'CursorStore']} })
    names: Optional[list[str]] = Field(default=None, description="""Element names, or glob patterns over them (`ingest_*`). Matched against the names the feature-file declares.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Selector']} })
    model: Optional[str] = Field(default=None, description="""Match every projection that folds into this model. The convenient and correct way to satisfy the rule that one model's projections may never be split across groups.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Selector']} })


class RetryPolicy(ConfiguredBaseModel):
    """
    Failure handling for a group. Retries are a transport-level concern and must not be confused with the log: a retried unit of work re-reads the same facts, and a permanently failed one is recorded, not silently dropped.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/topology'})

    max_attempts: Optional[int] = Field(default=None, description="""Attempts before the unit is treated as failed. Defaults to 1.""", ge=1, json_schema_extra = { "linkml_meta": {'domain_of': ['RetryPolicy']} })
    backoff: Optional[BackoffKind] = Field(default=None, description="""How the delay grows between attempts. Defaults to `none`.""", json_schema_extra = { "linkml_meta": {'domain_of': ['RetryPolicy']} })
    initial_delay_ms: Optional[int] = Field(default=None, description="""Delay before the second attempt. Ignored when backoff is `none`.""", ge=0, json_schema_extra = { "linkml_meta": {'domain_of': ['RetryPolicy']} })
    dead_letter: Optional[bool] = Field(default=None, description="""Whether a permanently failed unit is recorded for operator attention. Fan-out workloads need this: one bad artifact in a thousand must be findable without reading a log file.""", json_schema_extra = { "linkml_meta": {'domain_of': ['RetryPolicy']} })


class Transport(ConfiguredBaseModel):
    """
    The doorbell. Interchangeable by design — because correctness never rests on delivery, a brokerless transport and a broker are the same choice.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/topology'})

    kind: TransportKind = Field(default=..., description="""Which transport carries the wake-up.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Selector', 'Transport', 'CursorStore']} })
    url: Optional[str] = Field(default=None, description="""Connection string, when the kind needs one. Ignored by `inprocess`.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Transport', 'CursorStore']} })


class CursorStore(ConfiguredBaseModel):
    """
    Where each group's position in the event log is persisted.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/topology'})

    kind: CursorStoreKind = Field(default=..., description="""Which store holds cursors.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Selector', 'Transport', 'CursorStore']} })
    url: Optional[str] = Field(default=None, description="""Connection string or path, when the kind needs one.""", json_schema_extra = { "linkml_meta": {'domain_of': ['Transport', 'CursorStore']} })


class CommandPriority(ConfiguredBaseModel):
    """
    Which command TYPES are claimed ahead of the rest. There are exactly two levels — interactive and batch — and everything this class does not name is batch, so `interactive:` is the whole of the declaration.
    Two levels, and not a number, because a number has no statable semantics: a scheduler whose order depends on an operator's arithmetic can only be described by running it, and every such field ends in a tuning war between teams bidding their own work up. Two levels can be stated in one sentence — an interactive command is claimed before any batch command waiting on the same lane — and that sentence is testable.
    Priority is a property of the command TYPE, never of the chain that produced it. A policy reacting to an interactive command's event dispatches whatever that command type is declared to be; inheritance along the causal chain would make a command's latency depend on the route it arrived by, which is the thing a topology exists to state rather than to discover under load.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/topology'})

    interactive: Optional[list[str]] = Field(default=None, description="""Command names, or glob patterns over them (`approve_*`), matched against the commands the feature-file's procedures declare. These are `command:` values and not procedure names — a topology's `include` selectors name procedures, this names what they consume, and in a feature-file where procedure `open_batch` handles command `start_batch` the difference is visible. Naming something the feature-file declares as neither is a generation error.""", json_schema_extra = { "linkml_meta": {'domain_of': ['CommandPriority']} })


class GroupDefaults(ConfiguredBaseModel):
    """
    Values every group inherits unless it overrides them.
    """
    linkml_meta: ClassVar[LinkMLMeta] = LinkMLMeta({'from_schema': 'https://example.org/dizzy/topology'})

    workers: Optional[int] = Field(default=None, description="""Default worker count for a group that does not state one.""", ge=1, json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup', 'GroupDefaults']} })
    max_in_flight: Optional[int] = Field(default=None, description="""Default in-flight bound for a group that does not state one.""", ge=1, json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup', 'GroupDefaults']} })
    retry: Optional[RetryPolicy] = Field(default=None, description="""Default retry policy for a group that does not state one.""", json_schema_extra = { "linkml_meta": {'domain_of': ['ConsumerGroup', 'GroupDefaults']} })


# Model rebuild
# see https://pydantic-docs.helpmanual.io/usage/models/#rebuilding-a-model
Topology.model_rebuild()
ConsumerGroup.model_rebuild()
Selector.model_rebuild()
RetryPolicy.model_rebuild()
Transport.model_rebuild()
CursorStore.model_rebuild()
CommandPriority.model_rebuild()
GroupDefaults.model_rebuild()
