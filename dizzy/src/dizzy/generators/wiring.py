"""Wiring generator — lib/<runtime>/wiring/, the binding from elements to the runtime.

This is the last artifact that used to be copied out of the feature-file by hand.
The feat already declares, per element, exactly the argument list of its generated
context: a procedure's ``command:`` gives what it registers under, ``emits:`` the
emitter bindings, ``queries:`` the query bindings, ``environment:``/``telemetry:``
the rest; a projection's ``event:`` gives the event->projection routing; a policy's
``event:``/``emits:`` give its registration and its dispatch targets. Every line of
a hand-written wiring is that one template repeated, which makes it a pure function
of the feat — so it is generated, and cannot drift.

What is emitted is **engine-mediated**: emitters go to ``engine.emit_event`` and a
policy's dispatch to ``engine.dispatch_command``, never to another element. The
engine then owns the ordering rule (projections fold and the read model commits
before policies dispatch) rather than each handler restating it.
"""

from fnmatch import fnmatchcase
from pathlib import Path

from linkml_runtime.utils.formatutils import camelcase

from dizzy.feat_schema import (
    FeatureDefinition,
    PolicyDef,
    ProcedureDef,
    ProjectionDef,
    QueryDef,
)
from dizzy.logger import logger
from dizzy.topology_loader import group_of
from dizzy.topology_schema import Topology

WIRING_PACKAGE = "wiring"
"""Workspace member name, and the importable module (``from wiring import ...``)."""


def _call(name: str, args: list[list[str]], indent: str) -> list[str]:
    """Render ``name(...)`` across lines, one argument per line.

    *args* is a list of already-rendered argument blocks (each itself a list of
    lines). Generated code is read far more often than it is written — a wiring
    is the first place someone looks when an element is not firing — so it is
    emitted wrapped rather than as one unreadable line.
    """
    if not args:
        return [f"{indent}{name}()"]
    lines = [f"{indent}{name}("]
    for block in args:
        lines.extend(block)
    lines.append(f"{indent})")
    return lines


def _kwargs_call(name: str, kwargs: list[tuple[str, str]], indent: str) -> list[str]:
    """Render ``name(k=v, ...)`` with one keyword per line."""
    if not kwargs:
        return [f"{indent}{name}()"]
    lines = [f"{indent}{name}("]
    lines += [f"{indent}    {key}={value}," for key, value in kwargs]
    lines.append(f"{indent})")
    return lines


def _context_block(
    element: ProcedureDef | PolicyDef,
    emit_target: str,
    indent: str,
) -> list[str]:
    """The generated context construction for a procedure or a policy.

    *emit_target* is what every emitter binds to — ``engine.emit_event`` for a
    procedure (its output is a fact) or ``engine.dispatch_command`` for a policy
    (its output is an intent). That single substitution is the whole difference
    between the two loops.
    """
    name = element.name
    args: list[list[str]] = []
    inner = indent + "    "

    emit_lines = _kwargs_call(
        f"{name}_emitters", [(e, emit_target) for e in element.emits or []], inner
    )
    args.append(_prefix_first(emit_lines, "emit=", inner))

    if element.queries:
        query_lines = _kwargs_call(
            f"{name}_queries", [(q, f"queries.{q}") for q in element.queries], inner
        )
        args.append(_prefix_first(query_lines, "query=", inner))
    args.extend(_extras_blocks(name, element.environment, element.telemetry, inner))
    return _call(f"{name}_context", args, indent)


def _prefix_first(lines: list[str], prefix: str, indent: str) -> list[str]:
    """Turn a rendered call into a keyword argument, and comma-terminate it."""
    out = list(lines)
    out[0] = f"{indent}{prefix}{out[0].lstrip()}"
    out[-1] = out[-1] + ","
    return out


def _extras_blocks(
    name: str,
    environment: list[str] | None,
    telemetry: list[str] | None,
    indent: str,
) -> list[list[str]]:
    """The ``env=`` / ``telemetry=`` arguments, emitted only when declared."""
    blocks: list[list[str]] = []
    if environment:
        lines = _kwargs_call(
            f"{name}_env", [(e, f"resources.env.{e}") for e in environment], indent
        )
        blocks.append(_prefix_first(lines, "env=", indent))
    if telemetry:
        lines = _kwargs_call(
            f"{name}_telemetry",
            [(s, f"resources.telemetry.{s}") for s in telemetry],
            indent,
        )
        blocks.append(_prefix_first(lines, "telemetry=", indent))
    return blocks


def _leaf_context_block(
    element: ProjectionDef | QueryDef, adapter_expr: str, indent: str
) -> list[str]:
    """The generated context for a projection or a querier.

    These take an adapter rather than emitters: they read or write a model, they
    do not produce facts.
    """
    inner = indent + "    "
    args: list[list[str]] = []
    if element.adapter is not None:
        args.append([f"{inner}adapter={adapter_expr},"])
    args.extend(_extras_blocks(element.name, element.environment, element.telemetry, inner))
    return _call(f"{element.name}_context", args, indent)


def _render_imports(feat: FeatureDefinition) -> list[str]:
    """Every import the wiring needs, grouped: runtime, contracts, contexts, elements."""
    procedures = feat.procedures or []
    policies = feat.policies or []
    projections = feat.projections or []
    queries = feat.queries or []

    commands = sorted({p.command for p in procedures})
    events = sorted({p.event for p in projections} | {p.event for p in policies})

    lines = [
        "from __future__ import annotations",
        "",
        "from collections.abc import Callable, Mapping",
        "from dataclasses import dataclass, field",
        "from pathlib import Path",
        "from typing import Any",
        "",
        "from dizzy.engine import (",
        "    Engine,",
        "    EventStore,",
        "    FeatGraph,",
        "    HostApp,",
        "    Runtime,",
        "    ShellServices,",
        ")",
        "",
    ]

    if commands:
        lines.append("from gen_def.pydantic.commands import (")
        lines += [f"    {camelcase(c)}," for c in commands]
        lines.append(")")
    if events:
        lines.append("from gen_def.pydantic.events import (")
        lines += [f"    {camelcase(e)}," for e in events]
        lines.append(")")

    # No adapter imports: a context receives an adapter INSTANCE, which the host
    # supplies and the wiring looks up by name (``resources.adapter("sqla")``).
    # Importing the class would be an unused import in every generated module.

    for proc in procedures:
        names = [f"{proc.name}_context", f"{proc.name}_emitters"]
        if proc.queries:
            names.append(f"{proc.name}_queries")
        if proc.environment:
            names.append(f"{proc.name}_env")
        if proc.telemetry:
            names.append(f"{proc.name}_telemetry")
        lines.append(f"from gen_int.python.procedure.{proc.name}_context import (")
        lines += [f"    {n}," for n in names]
        lines.append(")")

    for policy in policies:
        names = [f"{policy.name}_context", f"{policy.name}_emitters"]
        if policy.queries:
            names.append(f"{policy.name}_queries")
        if policy.environment:
            names.append(f"{policy.name}_env")
        if policy.telemetry:
            names.append(f"{policy.name}_telemetry")
        lines.append(f"from gen_int.python.policy.{policy.name}_context import (")
        lines += [f"    {n}," for n in names]
        lines.append(")")

    for proj in projections:
        names = [f"{proj.name}_context"]
        if proj.environment:
            names.append(f"{proj.name}_env")
        if proj.telemetry:
            names.append(f"{proj.name}_telemetry")
        lines.append(f"from gen_int.python.projection.{proj.name}_projection import (")
        lines += [f"    {n}," for n in names]
        lines.append(")")

    for query in queries:
        names = [f"{query.name}_context"]
        if query.environment:
            names.append(f"{query.name}_env")
        if query.telemetry:
            names.append(f"{query.name}_telemetry")
        lines.append(f"from gen_int.python.query.{query.name} import (")
        lines += [f"    {n}," for n in names]
        lines.append(")")

    lines.append("")
    lines.append("# The element implementations — each is its own workspace package.")
    for element in list(procedures) + list(policies) + list(projections) + list(queries):
        lines.append(f"from {element.name} import {element.name}")

    return lines


_PREAMBLE = '''# AUTO-GENERATED — do not edit. Regenerate with `dizzy generate wiring`.
"""Wiring for the {feat_name} feature: elements bound to a DIZZY engine.

Generated from {feat_file}. Every registration below is a line of that file read
back out, which is why editing this module is the wrong move — the next
regeneration discards it. To specialize one element's binding, pass an override:

    build_engine(queue, store, Resources(adapters=..., overrides={{"{example}": my_runner}}))

The wiring is engine-mediated. A procedure's emitters go to ``engine.emit_event``
and a policy's to ``engine.dispatch_command``, so no element ever calls another;
the engine appends each event, folds its projections, commits, and only then lets
policies dispatch. A dispatched command lands on the queue as the next unit of
work — draining that queue is the scheduling shell's job, not this module's.
"""
'''


def _render_resources(feat: FeatureDefinition) -> list[str]:
    """The one thing a host must supply, and the doc explaining each field."""
    adapters = sorted(
        {
            e.adapter
            for e in list(feat.projections or []) + list(feat.queries or [])
            if e.adapter is not None
        }
    )
    env_names = [e.name for e in feat.environment or []]
    telemetry_names = [t.name for t in feat.telemetry or []]

    adapter_doc = (
        "Adapter instances by name: " + ", ".join(f"``{a}``" for a in adapters) + "."
        if adapters
        else "Adapter instances by name. This feature declares none."
    )
    env_doc = (
        "An object with one attribute per declared environment entry ("
        + ", ".join(f"``{e}``" for e in env_names)
        + ")."
        if env_names
        else "Unused: this feature declares no environment."
    )
    telemetry_doc = (
        "An object with one attribute per declared telemetry sink ("
        + ", ".join(f"``{t}``" for t in telemetry_names)
        + ")."
        if telemetry_names
        else "Unused: this feature declares no telemetry."
    )

    return [
        "@dataclass",
        "class Resources:",
        '    """What a host supplies so the declared elements can actually run."""',
        "",
        "    adapters: Mapping[str, Any] = field(default_factory=dict)",
        f'    """{adapter_doc}"""',
        "",
        "    env: Any = None",
        f'    """{env_doc}"""',
        "",
        "    telemetry: Any = None",
        f'    """{telemetry_doc}"""',
        "",
        "    overrides: Mapping[str, Callable] = field(default_factory=dict)",
        '    """Element name -> replacement runner. The supported way to specialize a',
        "    binding: a capability-pooled element a node must not import, a query that",
        "    needs a short-lived session. Forking this module to get the same effect",
        '    throws away the guarantee that it matches the feat."""',
        "",
        "    adapter_for: Callable[[str, Any], Any] | None = None",
        '    """Optional per-call adapter factory, ``(adapter_name, ingested_at) ->',
        "    adapter``. Supply it when a projection must see the event's stream-append",
        "    time; otherwise the static instance from ``adapters`` is used for every",
        '    fold."""',
        "",
        "    def adapter(self, name: str, ingested_at: Any = None) -> Any:",
        "        if self.adapter_for is not None:",
        "            return self.adapter_for(name, ingested_at)",
        "        return self.adapters[name]",
        "",
    ]


def _render_queries(feat: FeatureDefinition) -> list[str]:
    queries = feat.queries or []
    lines = [
        "@dataclass",
        "class Queries:",
        '    """Every declared query, bound to the read adapter.',
        "",
        "    Procedures and policies receive these through their generated context, so",
        '    a query is called with its input alone — the adapter is already bound."""',
        "",
    ]
    if queries:
        lines += [f"    {q.name}: Callable[[Any], Any]" for q in queries]
    else:
        lines.append("    pass")
    lines += [
        "",
        "",
        "def build_queries(resources: Resources) -> Queries:",
        '    """Bind every querier over the read adapter its model declares."""',
    ]
    if not queries:
        lines.append("    return Queries()")
        lines.append("")
        return lines
    for query in queries:
        ctx = _leaf_context_block(query, f'resources.adapter("{query.adapter}")', "            ")
        lines.append(f"    def _{query.name}(inp: Any) -> Any:")
        lines.append(f"        return {query.name}(")
        lines.append("            inp,")
        lines.extend(ctx[:-1])
        lines.append(ctx[-1] + ",")
        lines.append("        )")
        lines.append("")
    lines.append("    return Queries(")
    for query in queries:
        lines.append(
            f'        {query.name}=resources.overrides.get("{query.name}", _{query.name}),'
        )
    lines.append("    )")
    lines.append("")
    return lines


def _render_projection_runners(feat: FeatureDefinition) -> list[str]:
    """The data loop, as a map the engine, a rebuild and a replication all share."""
    projections = feat.projections or []
    lines = [
        "def build_projection_runners(",
        "    resources: Resources,",
        ") -> dict[type, list[tuple[str, Callable]]]:",
        '    """Event class -> ``[(name, runner)]``, the shape the engine registers.',
        "",
        "    The same map drives ``dizzy.engine.rebuild.rebuild`` and",
        "    ``replicate.fold_envelopes``, so one registration serves all three",
        '    triggers and a refold cannot fold a different set than the engine."""',
    ]
    if not projections:
        lines += ["    return {}", ""]
        return lines

    for proj in projections:
        ctx = _leaf_context_block(
            proj, f'resources.adapter("{proj.adapter}", ingested_at)', "            "
        )
        lines.append(f"    def _{proj.name}(event: Any, ingested_at: Any = None) -> None:")
        lines.append(f"        {proj.name}(")
        lines.append("            event,")
        lines.extend(ctx[:-1])
        lines.append(ctx[-1] + ",")
        lines.append("        )")
        lines.append("")

    lines.append("    runners: dict[type, list[tuple[str, Callable]]] = {}")
    for proj in projections:
        event_class = camelcase(proj.event)
        lines.append(
            f"    runners.setdefault({event_class}, []).append("
            f'("{proj.name}", resources.overrides.get("{proj.name}", _{proj.name})))'
        )
    lines += ["    return runners", ""]
    return lines


def _render_placement(feat: FeatureDefinition, topology: Topology | None) -> list[str]:
    """Command class name -> ``(lane, priority)``, read out of topology.yaml at build time.

    Placement is generated DATA and never a hand-written callable. A callable is a
    second place the deployment is described, and it drifts from the topology
    silently — the defect ``mp``'s hand-written ``routes()`` already demonstrates.

    Keyed on the command CLASS name because that is exactly ``type(command).__name__``:
    a shell reduces an instance to a key without importing a command class or
    consulting the graph, which is what keeps every command name out of
    ``dizzy/engine``. The lane is the claiming group's name — the executor decides
    how to serve each lane from ``LANES``, and a group's ``executor``/``workers`` are
    all it needs for that.

    The table has a shipped consumer: ``build_lane_executor`` below binds ``LANES``
    into a ``dizzy.engine.st.LaneExecutor`` and ``build_command_queue`` binds
    ``command_lane``/``command_priority`` into the durable queue. A host writes the
    LIFECYCLE — start, pump, stop — and never the placement.

    Keyed on the command rather than on the procedure, which is ambiguous exactly where
    the ENGINE is: two procedures declaring one ``command:`` leave ``Engine._procedures``
    one entry, so only one of them ever runs. This table keeps the first, and the pair
    belongs refused by the feature-file's own validation rather than decided here.

    A topology of ``None`` emits nothing at all: the default topology is one inline
    lane with every command batch, and a queue given no ``lane_of``/``priority_of``
    already behaves that way. Emitting a table that says so would be the same
    behaviour spelled out at the cost of making every topology-free wiring differ
    from the one shipped before topology existed.
    """
    if topology is None or not (topology.groups or []):
        return []

    claims = group_of(topology, feat)
    interactive = list((topology.priority.interactive if topology.priority else None) or [])

    # The command's own lane is its procedure's: a command is not in the log, so the
    # only thing that gives it a home is who runs it. A procedure `group_of` dropped
    # is one a validated topology cannot contain, and it is skipped rather than
    # guessed at.
    placements: dict[str, tuple[str, str]] = {}
    for proc in feat.procedures or []:
        lane = claims.get(("procedure", proc.name))
        if lane is None:
            continue
        priority = (
            "interactive"
            if any(fnmatchcase(proc.command, pattern) for pattern in interactive)
            else "batch"
        )
        placements.setdefault(camelcase(proc.command), (lane, priority))

    # An unplaced command must land somewhere a worker actually serves, so the fallback
    # is a declared group rather than the string "default", which a topology need not
    # have named. An inline lane is preferred because it is the one served in the host
    # process; a topology that declares only pools has no such lane, and naming its first
    # group is the honest answer rather than a lane nothing declares.
    groups = topology.groups or []
    fallback = next((g.name for g in groups if g.executor != "pool"), groups[0].name)

    lines = [
        "COMMAND_PLACEMENT: dict[str, tuple[str, str]] = {",
    ]
    lines += [
        f'    "{name}": ("{lane}", "{priority}"),'
        for name, (lane, priority) in sorted(placements.items())
    ]
    lines += [
        "}",
        '"""``type(command).__name__`` -> (lane, priority), straight out of topology.yaml.',
        "",
        "The lane is the name of the consumer group whose procedure runs the command;",
        "the priority is one of the two levels a topology may declare. For a shell to",
        "read, never an element. ``build_command_queue`` below binds it into the",
        'queue that claims by lane and priority."""',
        "",
        f'DEFAULT_PLACEMENT: tuple[str, str] = ("{fallback}", "batch")',
        '"""Where a command this table does not name goes: the first declared lane that is',
        "not a pool, or simply the first declared lane where a topology declares only",
        'pools."""',
        "",
        "LANES: dict[str, tuple[str, int]] = {",
    ]
    lines += [f'    "{g.name}": ("{g.executor or "inline"}", {g.workers or 1}),' for g in groups]
    lines += [
        "}",
        '"""Lane name -> (executor, workers). `inline` lanes are served in the host',
        "process and are the only ones that append to the log; a `pool` lane is served",
        'by a spawned process pool that many workers wide."""',
        "",
        "",
        "def placement_of(command: Any) -> tuple[str, str]:",
        '    """The (lane, priority) this topology puts *command* on."""',
        "    return COMMAND_PLACEMENT.get(type(command).__name__, DEFAULT_PLACEMENT)",
        "",
        "",
        "def command_lane(command: Any) -> str:",
        '    """``lane_of`` for a shell\'s command queue."""',
        "    return placement_of(command)[0]",
        "",
        "",
        "def command_priority(command: Any) -> int:",
        '    """``priority_of`` for a shell\'s command queue: 1 interactive, 0 batch.',
        "",
        "    The integers are the queue's own ``PRIORITY_INTERACTIVE``/``PRIORITY_BATCH``,",
        "    written out rather than imported so this module stays independent of which",
        '    shell reads it."""',
        '    return 1 if placement_of(command)[1] == "interactive" else 0',
        "",
        "",
        "def build_command_queue(path: Any = None, **kwargs: Any) -> Any:",
        '    """The durable command queue this topology describes.',
        "",
        "    Placement is never restated by hand: ``lane_of`` and ``priority_of`` are",
        "    the generated functions above, so there is no second description of the",
        "    deployment for the topology to drift from. *path* defaults to the queue's",
        "    own (``$DIZZY_COMMAND_QUEUE_PATH``).",
        "",
        "    The import is function-local on purpose. This module is shell-neutral —",
        "    an ``mp`` host imports the very same wiring — and a module-scope import of",
        '    the ``st`` shell would make every generated wiring pick a scheduler."""',
        "    from dizzy.engine.st import DurableCommandQueue",
        "",
        "    return DurableCommandQueue(",
        "        registry={c.__name__: c for c in feat_graph().commands.values()},",
        "        path=path,",
        "        lane_of=command_lane,",
        "        priority_of=command_priority,",
        "        **kwargs,",
        "    )",
        "",
        "",
        "def build_lane_executor(command_queue: Any, engine: Any, **kwargs: Any) -> Any:",
        '    """The executor that serves every lane this topology declares.',
        "",
        "    ``lanes=LANES`` is the one binding a host cannot get right by hand, so it",
        "    is bound here and nowhere else. What is NOT decided here is the",
        "    lifecycle: ``ordered``, ``backlog``, ``poll_interval``, ``owns_units`` and",
        "    when to start, pump and stop are deployment policy the topology never",
        "    states, so they stay the host's and pass through *kwargs*::",
        "",
        "        with build_lane_executor(queue, engine) as executor:",
        "            while running:",
        "                executor.pump()",
        "",
        '    Function-local import, for the same reason as above."""',
        "    from dizzy.engine.st import LaneExecutor",
        "",
        "    return LaneExecutor(command_queue, engine, lanes=LANES, **kwargs)",
        "",
    ]
    return lines


def _render_build_engine(feat: FeatureDefinition) -> list[str]:
    procedures = feat.procedures or []
    policies = feat.policies or []

    lines = [
        "def build_engine(",
        "    command_queue: Any,",
        "    store: EventStore | None,",
        "    resources: Resources,",
        "    observer: Callable[[str, Any], None] | None = None,",
        "    commit: Callable[[], None] | None = None,",
        "    bus: Callable[[dict], None] | None = None,",
        "    otel: Any = None,",
        ") -> Engine:",
        '    """Register every declared element and return the engine.',
        "",
        "    *command_queue* is the shell's: dispatches leave through it, and draining",
        "    it is the shell's job. *commit* is the read-model transaction boundary the",
        "    engine owns — it fires once per event, after the fold, before policies.",
        "",
        "    *store* may be None, which is what makes the procedure-only runtime below",
        "    possible: ``run_procedure`` reads neither the store nor the queue, so a",
        '    process that only runs procedures need open no log at all."""',
        "    engine = Engine(",
        "        command_queue=command_queue,",
        "        store=store,",
        "        observer=observer,",
        "        commit=commit,",
        "        bus=bus,",
        "        otel=otel,",
        "    )",
        "    queries = build_queries(resources)",
        "",
        "    # Data loop: one registration per declared event: projection edge.",
        "    for event_class, runners in build_projection_runners(resources).items():",
        "        for name, runner in runners:",
        "            engine.register_projection(event_class, runner, name=name)",
    ]

    if policies:
        lines += ["", "    # Reactivity loop: a policy's emits are COMMANDS, dispatched."]
    for policy in policies:
        ctx = _context_block(policy, "engine.dispatch_command", "            ")
        lines.append(f"    def _{policy.name}(event: Any) -> None:")
        lines.append(f"        {policy.name}(")
        lines.append("            event,")
        lines.extend(ctx[:-1])
        lines.append(ctx[-1] + ",")
        lines.append("        )")
        lines.append("")
        lines.append("    engine.register_policy(")
        lines.append(f"        {camelcase(policy.event)},")
        lines.append(f'        resources.overrides.get("{policy.name}", _{policy.name}),')
        lines.append(f'        name="{policy.name}",')
        lines.append("    )")

    if procedures:
        lines += ["", "    # Command handlers: a procedure's emits are EVENTS, appended."]
    for proc in procedures:
        ctx = _context_block(proc, "engine.emit_event", "            ")
        lines.append(f"    def _{proc.name}(command: Any) -> None:")
        lines.append(f"        {proc.name}(")
        lines.extend(ctx[:-1])
        lines.append(ctx[-1] + ",")
        lines.append("            command,")
        lines.append("        )")
        lines.append("")
        lines.append("    engine.register_procedure(")
        lines.append(f"        {camelcase(proc.command)},")
        lines.append(f'        resources.overrides.get("{proc.name}", _{proc.name}),')
        lines.append(f'        name="{proc.name}",')
        lines.append("    )")

    lines += ["", "    return engine", ""]
    return lines


def _render_host(feat_file_name: str) -> list[str]:
    """The feat graph, the two runtime builders, and the HostApp a shell resolves."""
    return [
        f'FEAT_FILE = Path(__file__).parent / "{feat_file_name}"',
        '"""The feature-file this wiring was generated from, shipped inside the',
        "package so a lifted-out ``lib/`` stays self-contained. It is a build",
        'artifact: regenerating the wiring refreshes it."""',
        "",
        "",
        "def feat_graph() -> FeatGraph:",
        '    """This feature\'s topology, every declared name resolved to its class."""',
        "    return FeatGraph.load(FEAT_FILE)",
        "",
        "",
        "def build_runtime(",
        "    services: ShellServices,",
        "    resources: Resources,",
        "    store: EventStore | None = None,",
        "    commit: Callable[[], None] | None = None,",
        "    session: Any = None,",
        "    refresh: Callable[[], None] = lambda: None,",
        ") -> Runtime:",
        '    """Build this process\'s engine around the services a shell provides.',
        "",
        "    The shell's observer is chained in, not replaced: a shell collects the",
        "    events a command emitted through it, so an app that installs its own",
        '    observer without chaining silently unplugs the shell."""',
        "    engine = build_engine(",
        "        services.command_queue,",
        "        store if store is not None else EventStore(graph=feat_graph()),",
        "        resources,",
        "        observer=services.observer,",
        "        commit=commit,",
        "    )",
        "    return Runtime(engine=engine, session=session, refresh=refresh)",
        "",
        "",
        "def build_procedure_runtime(",
        "    services: ShellServices,",
        "    resources: Resources | None = None,",
        "    refresh: Callable[[], None] = lambda: None,",
        ") -> Runtime:",
        '    """The runtime a POOL CHILD builds: procedures, and nothing that writes.',
        "",
        "    ``store=None`` is the whole point. A pool child appends nothing, folds",
        "    nothing and dispatches nothing, so it must not OPEN the host's log to",
        "    prove it — and the full builder above would, because its store falls back",
        "    to the ``$DIZZY_STORE_PATH`` the child inherited across the spawn.",
        "",
        "    The same ``build_engine`` is used rather than a second, smaller one: the",
        "    projections and policies it also registers are never reached, because a",
        "    child only ever calls ``run_procedure``, and sharing the builder is what",
        "    keeps the child's procedures from drifting from the host's.",
        "",
        "    *resources* still matters: environment and telemetry are read inside each",
        "    runner, per call. The default ``Resources()`` is therefore correct only",
        "    while nothing a pool lane runs reads them — bind your own otherwise. What",
        "    it never needs is ``adapters``: a pool group may hold no projection and no",
        "    element declaring ``queries:``, which topology validation enforces, so a",
        '    pool-eligible procedure reads no model through DIZZY."""',
        "    engine = build_engine(",
        "        services.command_queue,",
        "        None,",
        "        resources if resources is not None else Resources(),",
        "    )",
        "    return Runtime(engine=engine, refresh=refresh)",
        "",
        "",
        "def host_app(",
        "    build: Callable[[ShellServices], Runtime],",
        "    procedures: Callable[[ShellServices], Runtime] | None = None,",
        "    **kwargs: Any,",
        ") -> HostApp:",
        '    """Package a runtime builder as the HostApp a shell resolves from',
        "    ``$DIZZY_HOST_APP``. *build* is usually ``build_runtime`` with this host's",
        "    resources already bound::",
        "",
        "        app = host_app(lambda services: build_runtime(services, my_resources()))",
        "",
        "    *procedures* is what a spawned pool child builds instead. It defaults to",
        "    ``build_procedure_runtime`` with an empty ``Resources()``, which opens no",
        "    log at all; bind your own the same way as *build* when a pooled procedure",
        "    reads environment or telemetry::",
        "",
        "        app = host_app(",
        "            lambda services: build_runtime(services, res),",
        "            lambda services: build_procedure_runtime(services, res),",
        "        )",
        "",
        "    Extra keyword arguments (``routes``, ``otel``, ``on_command_done``, …) pass",
        '    straight through to ``HostApp``."""',
        "    return HostApp(",
        "        graph=feat_graph(),",
        "        build_runtime=build,",
        "        build_procedure_runtime=procedures or build_procedure_runtime,",
        "        **kwargs,",
        "    )",
        "",
    ]


def feat_name(feat_file_name: str) -> str:
    """``recipes.feat.yaml`` -> ``recipes``. The feature's name is its file's."""
    return feat_file_name.split(".")[0]


def render_wiring(
    feat: FeatureDefinition,
    feat_file: str = "feature.feat.yaml",
    topology: Topology | None = None,
) -> str:
    """Render lib/<runtime>/wiring/src/wiring.py for *feat*."""
    example = next(
        (e.name for e in list(feat.procedures or []) + list(feat.projections or [])),
        "element_name",
    )
    preamble = _PREAMBLE.format(
        feat_name=feat_name(feat_file), feat_file=feat_file, example=example
    )
    blocks = [
        preamble,
        "\n".join(_render_imports(feat)),
        "\n".join(_render_resources(feat)),
        "\n".join(_render_queries(feat)),
        "\n".join(_render_projection_runners(feat)),
        "\n".join(_render_placement(feat, topology)),
        "\n".join(_render_build_engine(feat)),
        "\n".join(_render_host(feat_file)),
    ]
    # Empty blocks are dropped rather than joined: a topology-free build emits no
    # placement table, and a blank block would still cost the module a blank line.
    return "\n\n".join(b for b in (block.rstrip() for block in blocks) if b) + "\n"


def render_wiring_pyproject_toml(feat: FeatureDefinition) -> str:
    """The wiring package manifest.

    This is the one generated package that depends on DIZZY itself: the elements
    import only their contracts, but the wiring imports ``dizzy.engine`` — it is
    what binds them to a runtime.

    The ``dizzy`` requirement is deliberately left un-sourced here. DIZZY is not
    published to a package index, so it needs a source — but that belongs at the
    workspace ROOT (see ``render_workspace_pyproject_toml``), where it applies to
    every member and can be pointed at a checkout or a git URL without rewriting
    this file. Keeping it out means this manifest is the same wherever the lib is
    shipped.
    """
    elements = (
        [("procedure", p.name) for p in feat.procedures or []]
        + [("policy", p.name) for p in feat.policies or []]
        + [("projection", p.name) for p in feat.projections or []]
        + [("query", q.name) for q in feat.queries or []]
    )
    deps = ['    "dizzy",', '    "gen_def",', '    "gen_int",'] + [
        f'    "{kind}-{name}",' for kind, name in elements
    ]
    sources = [
        "gen_def = { workspace = true }",
        "gen_int = { workspace = true }",
    ] + [f"{kind}-{name} = {{ workspace = true }}" for kind, name in elements]
    return "\n".join(
        [
            "[project]",
            'name = "wiring"',
            'version = "0.1.0"',
            'requires-python = ">=3.11"',
            "dependencies = [",
            *deps,
            "]",
            "",
            "[tool.uv.sources]",
            *sources,
            "",
            "[build-system]",
            'requires = ["hatchling"]',
            'build-backend = "hatchling.build"',
            "",
            "[tool.hatch.build.targets.wheel]",
            'sources = ["src"]',
            'include = ["src/wiring.py", "src/*.feat.yaml"]',
            "",
        ]
    )


def write_wiring_python_uv(
    feat: FeatureDefinition,
    feat_file: Path,
    output_dir: Path,
    topology: Topology | None = None,
) -> None:
    """Write lib/python-uv/wiring/ — always overwritten, like every generated interface."""
    base = output_dir / "lib" / "python-uv" / WIRING_PACKAGE
    (base / "src").mkdir(parents=True, exist_ok=True)
    (base / "pyproject.toml").write_text(render_wiring_pyproject_toml(feat))
    (base / "src" / "wiring.py").write_text(render_wiring(feat, feat_file.name, topology))
    # The feat travels with the package so a lifted-out lib/ can still read its
    # own topology; it is regenerated here, never edited in place.
    (base / "src" / feat_file.name).write_text(feat_file.read_text())
    logger.debug("wrote wiring package", extra={"path": str(base)})
