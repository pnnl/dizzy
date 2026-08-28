"""Topology loader — YAML ingestion, Pydantic validation, and cross-reference checks."""

from fnmatch import fnmatchcase
from pathlib import Path

import yaml

from dizzy.feat_schema import FeatureDefinition
from dizzy.logger import logger
from dizzy.topology_schema import ConsumerGroup, GroupDefaults, Selector, Topology

# The kinds a topology places, in declaration order. Queries are absent by construction:
# their placement is a question about the read substrate, not about the log (rule 4).
# Keyed by the plural string, not by ElementKind: the generated models set
# `use_enum_values = True`, so `selector.kind` is "projections", never the enum member.
_SINGULAR = {"procedures": "procedure", "policies": "policy", "projections": "projection"}

_GLOB_CHARS = "*?["


def _and_list(items: list[str]) -> str:
    """Join already-rendered *items* as ``a``, ``a and b``, ``a, b and c``."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _summarise(elements: list[tuple[str, str]], limit: int = 3) -> str:
    """Render up to *limit* elements as labels, counting the rest — a glob may match many."""
    shown = _and_list([_label(k, n) for k, n in elements[:limit]])
    remainder = len(elements) - limit
    return shown if remainder <= 0 else f"{shown} and {remainder} more"


def _label(kind: str, name: str) -> str:
    """Render an element as ``projection 'record_artifact'`` — every message's anchor."""
    return f"{kind} '{name}'"


def _universe(feat: FeatureDefinition) -> list[tuple[str, str]]:
    """Every placeable element as ``(kind, name)``, in kind then feat declaration order.

    An element is the pair, never the bare name: nothing in the feature-file enforces
    cross-kind name uniqueness, so a procedure and a projection may both be `ingest`.
    """
    sections = {
        "procedures": feat.procedures or [],
        "policies": feat.policies or [],
        "projections": feat.projections or [],
    }
    universe = [
        (singular, e.name) for plural, singular in _SINGULAR.items() for e in sections[plural]
    ]
    # Every map below is keyed on this pair, so a repeat would collapse two elements into
    # one and hide, say, a reading procedure behind a query-free namesake. `validate_feat`
    # refuses the duplicate; assert it here, where the collapse would actually happen.
    assert len(set(universe)) == len(universe), "feat declares one kind's name twice"
    return universe


def _models(feat: FeatureDefinition) -> dict[tuple[str, str], str | None]:
    """Map each element to the model it folds, or ``None`` for non-projections."""
    folds: dict[tuple[str, str], str | None] = dict.fromkeys(_universe(feat))
    for projection in feat.projections or []:
        folds[("projection", projection.name)] = projection.model
    return folds


def _matches(selector: Selector, kind: str, name: str, model: str | None) -> bool:
    """Whether *selector* claims the element ``(kind, name)`` folding *model*.

    The fields a selector states are ANDed; a field it omits constrains nothing. An
    element that lacks the field a selector states is a non-match, never a don't-care.
    """
    if selector.kind is not None and _SINGULAR[selector.kind] != kind:
        return False
    if selector.model is not None and (kind != "projection" or model != selector.model):
        return False
    # fnmatchcase, never fnmatch: the latter runs os.path.normcase, so the same topology
    # would validate differently on a case-insensitive filesystem.
    # `is not None`, not truthiness: an empty `names: []` states a name constraint that
    # nothing satisfies, so the selector claims nothing and Phase B says so. Reading it as
    # "unstated" would silently widen it to whatever its other fields allow — a file that
    # enumerates no name placing a whole kind.
    if selector.names is not None and not any(fnmatchcase(name, p) for p in selector.names):
        return False
    return True


def _inherited(group_value: int | None, default_value: int | None, field: str) -> tuple[int, str]:
    """Resolve one knob to ``(value, source)``, source naming where the value came from.

    The source is interpolated into the ordering errors so a topology-level default does
    not produce an unattributable message quoting a number the author never wrote there.
    """
    if group_value is not None:
        return group_value, field
    if default_value is not None:
        return default_value, f"defaults.{field}"
    return 1, field


def _effective_concurrency(group: ConsumerGroup, defaults: GroupDefaults | None) -> tuple[int, str]:
    """How many units of work a group may have in flight at once, and which field said so.

    Both knobs break log order the same way: N workers fold N events concurrently, and so
    does one worker holding N units in flight. The ordering gate therefore reads whichever
    is larger — a group is serial only when both are 1 — and reports the field the author
    wrote, so the message names something they can edit.
    """
    workers = _inherited(group.workers, defaults.workers if defaults else None, "workers")
    in_flight = _inherited(
        group.max_in_flight, defaults.max_in_flight if defaults else None, "max_in_flight"
    )
    return max(workers, in_flight, key=lambda pair: pair[0])


def _resolve(topology: Topology, feat: FeatureDefinition) -> dict[tuple[str, str], list[str]]:
    """Map every placeable element to the consumer groups that claim it.

    Explicit ``include`` selectors are applied first, then a single ``catch_all``
    group takes every element still unclaimed. An unclaimed element maps to an
    empty list; a doubly claimed one maps to two group names in declaration order.
    """
    groups = topology.groups or []
    universe = _universe(feat)
    element_models = _models(feat)
    claims: dict[tuple[str, str], list[str]] = {e: [] for e in universe}

    for group in groups:
        for kind, name in universe:
            selectors = group.include or []
            if any(_matches(s, kind, name, element_models[(kind, name)]) for s in selectors):
                claims[(kind, name)].append(group.name)

    # The sweep is defined by subtraction, so it runs strictly after every explicit
    # selector in every group. With two sweepers the subtraction is undefined — T6 has
    # already fired, and sweeping arbitrarily would manufacture double-claims on top.
    sweepers = [g for g in groups if g.catch_all]
    if len(sweepers) == 1:
        for claimants in claims.values():
            if not claimants:
                claimants.append(sweepers[0].name)

    return claims


def group_of(topology: Topology, feat: FeatureDefinition) -> dict[tuple[str, str], str]:
    """Map each element to the single group that claims it, dropping anything else.

    The public read of a *validated* topology, for a generator that has to turn
    placement into emitted data. `validate_topology` has already refused an element
    claimed twice or not at all, so the elements this drops are exactly the ones a
    build should never have got this far with.
    """
    claims = _resolve(topology, feat)
    return {element: c[0] for element, c in claims.items() if len(c) == 1}


def load_topology(path: str | Path) -> Topology:
    """Load a ``topology.yaml`` file, validate it, and return a Topology."""
    raw = yaml.safe_load(Path(path).read_text())
    logger.debug("loaded topology", extra={"path": str(path)})
    if raw is None:
        raw = {}
    normalised = dict(raw)
    # LinkML 1.11 stopped defaulting optional multivalued slots, and callers must never
    # distinguish "omitted" from "empty".
    normalised.setdefault("groups", [])
    return Topology.model_validate(normalised)


def validate_topology(topology: Topology, feat: FeatureDefinition) -> list[str]:
    """Validate a topology's element placement against the feature-file.

    Returns a list of error strings; empty list means the topology is valid. Checks run
    in three phases — structure, then references, then placement — and each returns
    early on failure: an incoherent topology makes reference checks meaningless, and a
    selector naming something the feat does not declare makes every placement
    conclusion noise about elements the author never meant to leave unplaced.
    """
    errors: list[str] = []
    groups = topology.groups or []

    # ---- Phase A: structure, the topology alone ----------------------------------
    if not groups:
        errors.append(
            "topology declares no consumer groups — every element would be unclaimed (rule 5)"
        )
        logger.debug("validated topology", extra={"errors": len(errors)})
        return errors

    seen: set[str] = set()
    for group in groups:
        if group.name in seen:
            errors.append(
                f"topology group '{group.name}' is declared more than once "
                "— group names must be unique"
            )
        seen.add(group.name)
        if not (group.include or []) and not group.catch_all:
            errors.append(
                f"topology group '{group.name}': no 'include' selectors and 'catch_all' "
                "is not set — the group claims nothing"
            )
        for i, selector in enumerate(group.include or [], start=1):
            if selector.kind is None and selector.names is None and selector.model is None:
                errors.append(
                    f"topology group '{group.name}': selector #{i} states none of 'kind', "
                    "'names' or 'model' — set 'catch_all: true' if the group should claim "
                    "the remainder"
                )
    sweepers = [g.name for g in groups if g.catch_all]
    if len(sweepers) > 1:
        rendered = _and_list([f"'{n}'" for n in sweepers])
        errors.append(
            f"topology groups {rendered} all set 'catch_all' — at most one group may (rule 5)"
        )

    if errors:
        logger.debug("validated topology", extra={"errors": len(errors)})
        return errors

    # ---- Phase B: references, and rule 4 -----------------------------------------
    universe = _universe(feat)
    element_models = _models(feat)
    model_names = {m.name for m in feat.models or []}
    folded = {p.model for p in feat.projections or [] if p.model}
    query_names = [q.name for q in feat.queries or []]
    kinds_of: dict[str, list[str]] = {}
    for kind, name in universe:
        kinds_of.setdefault(name, []).append(kind)

    for group in groups:
        for i, selector in enumerate(group.include or [], start=1):
            reported = False
            dead_globs: list[str] = []

            if selector.model is not None:
                if selector.kind is not None and selector.kind != "projections":
                    errors.append(
                        f"topology group '{group.name}': selector #{i} states model "
                        f"'{selector.model}' with kind '{selector.kind}' — only projections "
                        "fold a model, so the selector matches nothing"
                    )
                    reported = True
                elif selector.model not in model_names:
                    errors.append(
                        f"topology group '{group.name}': selector #{i} names model "
                        f"'{selector.model}' not declared in models"
                    )
                    reported = True
                elif selector.model not in folded:
                    errors.append(
                        f"topology group '{group.name}': selector #{i} names model "
                        f"'{selector.model}', which no projection folds — the selector "
                        "matches nothing"
                    )
                    reported = True

            # dict.fromkeys, not a set: a pattern written twice is one mistake with one
            # fix, and the order patterns were authored in is the order to report them in.
            for pattern in dict.fromkeys(selector.names or []):
                is_glob = any(c in pattern for c in _GLOB_CHARS)
                any_kind = [e for e in universe if fnmatchcase(e[1], pattern)]

                if selector.kind is None and selector.model is None:
                    # The only reachable shape of rule 4: a selector stating a kind or a
                    # model provably cannot claim a query, so flagging it would be false.
                    # The literal/glob split is rule 4's own second sentence: an exact name
                    # that also names a placeable element claims that element, a glob that
                    # sweeps a query name is refused even when it matches elements too.
                    hits = [q for q in query_names if fnmatchcase(q, pattern)]
                    if hits and (is_glob or not any_kind):
                        for query in hits:
                            errors.append(
                                f"topology group '{group.name}': selector #{i} matches query "
                                f"'{query}' — queries are never claimed by a consumer group "
                                "(rule 4)"
                            )
                        reported = True
                        # "not a declared element" would be actively wrong about a name
                        # the feat declares as a query.
                        continue

                if not any_kind:
                    if not is_glob:
                        errors.append(
                            f"topology group '{group.name}': selector #{i} names '{pattern}' "
                            "not declared in procedures, policies or projections"
                        )
                        reported = True
                    else:
                        # A glob matching nothing today is not an error on its own:
                        # matching by property is what lets a topology survive the
                        # feature-file gaining an element next week. It is only worth
                        # saying when the whole selector turned out to claim nothing,
                        # so the message below can name the pattern that went dead.
                        dead_globs.append(pattern)
                elif selector.kind is not None and not any(
                    e[0] == _SINGULAR[selector.kind] for e in any_kind
                ):
                    # Reported for a glob too: a sibling pattern that does match would
                    # otherwise swallow this one, and the elements the author meant to
                    # place would drift into the catch_all group with nothing said.
                    if is_glob:
                        errors.append(
                            f"topology group '{group.name}': selector #{i} pattern '{pattern}' "
                            f"with kind '{selector.kind}' matches no "
                            f"{_SINGULAR[selector.kind]} — it matches only "
                            f"{_summarise(any_kind)}, so the pattern claims nothing"
                        )
                    else:
                        declared = _and_list([f"a {k}" for k in kinds_of[pattern]])
                        errors.append(
                            f"topology group '{group.name}': selector #{i} names '{pattern}' "
                            f"with kind '{selector.kind}', but '{pattern}' is declared as "
                            f"{declared}"
                        )
                    reported = True

            if not reported and not [
                e for e in universe if _matches(selector, e[0], e[1], element_models[e])
            ]:
                rendered = _and_list([f"'{p}'" for p in dead_globs])
                detail = ""
                if len(dead_globs) == 1:
                    detail = f"; pattern {rendered} matches nothing"
                elif dead_globs:
                    detail = f"; patterns {rendered} match nothing"
                errors.append(
                    f"topology group '{group.name}': selector #{i} claims nothing "
                    f"— no element satisfies all of the fields it states{detail}"
                )

    # Priority is checked here, with the other reference checks, and against a
    # DIFFERENT name-space: `include` selectors name procedures, `priority` names the
    # commands they consume. Where a feature-file has procedure `open_batch` handling
    # command `start_batch`, an author who guesses wrong gets a list that silently
    # prioritises nothing, so the guess is refused by name.
    interactive = list((topology.priority.interactive if topology.priority else None) or [])
    commands = {p.command for p in feat.procedures or []}
    for pattern in dict.fromkeys(interactive):
        if any(c in pattern for c in _GLOB_CHARS):
            # Tolerated for the same reason a dead selector glob is: matching by property
            # is what lets a topology survive the feature-file gaining a command. The
            # whole-list check below is what refuses a declaration that does nothing.
            continue
        if pattern not in commands:
            errors.append(
                f"topology priority.interactive names '{pattern}', which no procedure "
                "declares as its command — priority keys on command names, not on the "
                "names of the procedures that decide them"
            )
    if interactive and not any(
        fnmatchcase(c, pattern) for c in commands for pattern in interactive
    ):
        rendered = _and_list([f"'{p}'" for p in dict.fromkeys(interactive)])
        errors.append(
            f"topology priority.interactive lists {rendered} and matches no command — "
            "every command would be batch, which is what omitting the section says"
        )

    if errors:
        logger.debug("validated topology", extra={"errors": len(errors)})
        return errors

    # ---- Phase C: placement -------------------------------------------------------
    claims = _resolve(topology, feat)
    # Sole membership: a doubly claimed element belongs to nobody, so it cannot cascade a
    # rule-1 or workers error on top of the one message that names its real problem.
    members = {g.name: [e for e in universe if claims[e] == [g.name]] for g in groups}

    for kind, name in universe:
        claimants = claims[(kind, name)]
        if len(claimants) >= 2:
            rendered = _and_list([f"'{c}'" for c in claimants])
            errors.append(
                f"{_label(kind, name)} is claimed by topology groups {rendered} — every "
                "element must be claimed by exactly one group (rule 5)"
            )
        elif not claimants:
            errors.append(
                f"{_label(kind, name)} is claimed by no topology group, and no group sets "
                "'catch_all' (rule 5)"
            )

    # `owner` is rule 6's half of this loop: the group a model's fold lives in, which is
    # only a fact once rule 1 holds. A model whose cohort straddles two groups, or whose
    # projections were all dropped by the sole-membership filter above, has no owner —
    # and a model no projection folds never enters the map at all.
    owner: dict[str, str] = {}
    for model in feat.models or []:
        cohort = [
            p
            for p in feat.projections or []
            if p.model == model.name and len(claims[("projection", p.name)]) == 1
        ]
        holders = {claims[("projection", p.name)][0] for p in cohort}
        if len(holders) > 1:
            placements = _and_list(
                [f"'{p.name}' (group '{claims[('projection', p.name)][0]}')" for p in cohort]
            )
            errors.append(
                f"model '{model.name}': projections {placements} fold one model in different "
                "consumer groups — one model's projections must share a group (rule 1)"
            )
        elif holders:
            owner[model.name] = next(iter(holders))

    # The reader map, keyed on the `queries:` list and never on the kind that declares it:
    # rule 2 exonerates a policy for observing events out of order, which says nothing
    # about the model it reads, and the generated policy context carries a real `query:`
    # handle. Keyed on the (kind, name) PAIR because nothing makes names unique across
    # kinds. Rules 3 and 6 are the same fact on two axes and read it from here both times.
    reads: dict[tuple[str, str], list[str]] = {
        ("procedure", p.name): list(p.queries or []) for p in feat.procedures or []
    }
    reads.update({("policy", y.name): list(y.queries or []) for y in feat.policies or []})

    # Rule 6. Rule 3 pins a reader's group to one lane; this pins the reader to one GROUP —
    # the one folding what it reads. The asymmetry is the point: what the log records about
    # a read is the DECISION, never the model, so a reader whose group trails the fold it
    # reads does not emit the same event late, it emits a DIFFERENT one, and no refold puts
    # back an entry the log is short.
    #
    # Reported per (element, model), not per query: two queries over one model are one
    # placement mistake with one fix, and the fix names the model's group.
    #
    # Silent wherever a group is not a fact. An element claimed twice or not at all has no
    # group and a model with no owner has no fold to be distant from — rules 5 and 1 have
    # already named those, and a second message about the same artifact is the cascade
    # `members` exists to prevent. A model no projection folds constrains nobody: nothing
    # here folds it, so no placement of this feature-file can put a reader behind it.
    query_model = {q.name: q.model for q in feat.queries or []}
    for kind, name in universe:
        claimants = claims[(kind, name)]
        if len(claimants) != 1:
            continue
        reached: dict[str, list[str]] = {}
        # dict.fromkeys, not a set: one query written twice is one read, and the order the
        # `queries:` list was authored in is the order to report the models it reaches in.
        for query in dict.fromkeys(reads.get((kind, name)) or []):
            model_name = query_model.get(query)
            if model_name is None or owner.get(model_name) in (None, claimants[0]):
                continue
            reached.setdefault(model_name, []).append(query)
        for model_name, queried in reached.items():
            rendered = _and_list([f"'{q}'" for q in queried])
            errors.append(
                f"{_label(kind, name)} is claimed by topology group '{claimants[0]}' but "
                f"queries {rendered} over model '{model_name}', whose projections group "
                f"'{owner[model_name]}' claims — what the log records is the decision and "
                "never the model, so a reader trailing the fold it reads emits a different "
                f"event that no refold repairs; it must be claimed by '{owner[model_name]}' "
                "(rule 6)"
            )

    # Rules 2 and 3. An element partitions when it reads nothing: declaring no queries, it
    # computes its output from the command or event alone, so no concurrent decision can
    # reach it. One that DOES query is a read-modify-write over a model a peer may be
    # changing underneath it, and the feature-file declares no aggregate key to serialise
    # it by — so that one, and only that one, is what forces a group serial. Rule 6 has
    # already put that reader in the group folding what it reads; this says how many lanes
    # that group may then run, and it reads the same `reads` map to decide.
    for group in groups:
        count, source = _effective_concurrency(group, topology.defaults)
        # A background lane runs while the main lane runs, so `executor: pool` is a
        # concurrency claim exactly as `workers: 4` is — one gate, one set of messages.
        # It is reported in preference to a worker count because it is the stronger
        # statement: dropping to one pool worker would not put the group back on the
        # lane that folds. The eligibility question is already answered by rule 3, and a
        # second, pool-specific message would be a second answer to it.
        if group.executor == "pool":
            shown, source, serial = "pool", "executor", "executor: inline"
        elif count > 1:
            shown, serial = str(count), "workers: 1 and max_in_flight: 1"
        else:
            continue
        mine = members[group.name]
        projection = next((e for e in mine if e[0] == "projection"), None)
        reader = next((e for e in mine if reads.get(e)), None)
        if projection is not None:
            errors.append(
                f"topology group '{group.name}': {source} is {shown}, but the group claims "
                f"projection '{projection[1]}' — a fold is only correct in log order, so a "
                f"group containing a projection must run serially ({serial})"
            )
        elif reader is not None:
            queried = _and_list([f"'{q}'" for q in reads[reader]])
            errors.append(
                f"topology group '{group.name}': {source} is {shown}, but the group claims "
                f"{_label(*reader)}, which queries {queried} — it decides on a model a "
                "concurrent peer may be changing underneath it, and the feature-file "
                "declares no aggregate key to serialise it by, so it must run serially "
                f"({serial})"
            )
        elif not mine:
            errors.append(
                f"topology group '{group.name}': {source} is {shown}, but the group claims no "
                "element — there is nothing here to scale"
            )

    logger.debug("validated topology", extra={"errors": len(errors)})
    return errors
