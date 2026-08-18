"""The feat-driven app graph — what an engine shell needs to know, read.

DIZZY's thesis is that the design lives in the artifact: ``<name>.feat.yaml``
declares the whole topology (commands, events, procedures, projections,
policies, queries, environment, telemetry). Everything a *scheduling shell*
needs about an app is therefore derivable — a shell should never hard-code a
command name, an event name, or an environment field.

``FeatGraph`` is that derivation. Given a feat path (found by env var or an
upward search) it resolves every declared name to its generated pydantic class
by DIZZY's own naming convention (``classify_image`` → ``ClassifyImage`` in
``gen_def.pydantic.commands``), and fails loudly when the feat declares
something the generated packages don't provide — a stale-generation check a
hand-maintained command dict cannot make, since a missing entry there is
indistinguishable from a command nobody dispatches.

Resolution is LAZY per section: a producer-only process (enqueue without
building the library) resolves ``commands`` and never imports the events
module; a worker resolves both. ``environment``/``telemetry`` need no import
at all — their names alone are the answer.

Costs pyyaml and nothing else. That is deliberate: a worker process installs
DIZZY to get a scheduling shell, and the generator's tree (linkml, openai,
typer) lives behind the ``gen`` extra so it never rides along.
"""

from __future__ import annotations

import importlib
import os
import re
from functools import cached_property
from pathlib import Path
from typing import Any

# The sections of a feat file this graph understands. Order is the feat's own.
TOPOLOGY_SECTIONS = (
    "commands",
    "events",
    "procedures",
    "projections",
    "policies",
    "queries",
)

# Every section a feat may declare — TOPOLOGY_SECTIONS plus the ones that carry
# shapes rather than elements. Anything outside this set is not a section, and
# asking for one is an error rather than an empty answer.
SECTIONS = TOPOLOGY_SECTIONS + ("models", "environment", "telemetry")

# Sections whose entries have a generated pydantic class, and the module (under
# the generated definitions package) that class lives in.
_CLASS_SECTIONS = {
    "commands": "commands",
    "events": "events",
    "environment": "environment",
    "telemetry": "telemetry",
}

DEFAULT_DEF_PACKAGE = "gen_def.pydantic"


def camel_case(name: str) -> str:
    """``classify_image`` -> ``ClassifyImage``.

    LinkML's ``camelcase`` semantics (that generator produced the classes, so
    this must match it, not merely resemble it): split on non-word runs and
    underscores, upper the first character of each part, keep the rest.
    """
    return "".join(f"{p[0].upper()}{p[1:]}" for p in re.split(r"[\W_]+", name) if p)


def snake_case(name: str) -> str:
    """``ClassifyImage`` -> ``classify_image`` — the inverse of camel_case."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def check_name(name: Any, section: str, feat_name: str) -> str:
    """Reject a declared name that does not survive the round trip.

    ``camel_case`` splits on any non-word run, so ``classify-image``,
    ``classifyImage`` and ``classify_image`` all collapse to ``ClassifyImage``.
    Left unchecked, a typo'd feat entry resolves to its NEIGHBOUR's class and
    the reverse lookup (which routes commands) silently maps it back to the
    wrong name — the stale-generation check would pass on a broken feat. A
    name is only well formed if ``snake_case(camel_case(name)) == name``.

    Also catches YAML's scalar keys: ``123:`` or ``on:`` parse to int/bool and
    would otherwise die inside ``re`` with no mention of the feat file.
    """
    if not isinstance(name, str):
        raise RuntimeError(
            f"{feat_name}: {section} declares a non-string name {name!r} "
            f"({type(name).__name__}) — quote it (YAML reads 123, yes, on, off "
            f"as scalars)"
        )
    if not name or snake_case(camel_case(name)) != name:
        raise RuntimeError(
            f"{feat_name}: {section} declares {name!r}, which is not "
            f"snake_case — it would resolve to {camel_case(name)!r}, the same "
            f"class as {snake_case(camel_case(name))!r}"
        )
    return name


def find_feat(start: Path | None = None) -> Path:
    """Locate the app's feat file.

    ``$DIZZY_FEAT_PATH`` wins. Otherwise walk up from *start* (default: the
    working directory) looking for exactly one ``*.feat.yaml``. This is how a
    worker boots knowing only where it is — no app import required.
    """
    env = os.environ.get("DIZZY_FEAT_PATH")
    if env is not None and env.strip() == "":
        # An exported-but-empty variable is a misconfiguration, not "unset":
        # falling through to the walk-up would silently pick up whatever feat
        # happens to be nearest, which is worse than failing.
        raise RuntimeError(
            "$DIZZY_FEAT_PATH is set but empty — unset it to "
            "search upward, or point it at a feat file"
        )
    if env:
        path = Path(env).expanduser()
        if not path.is_file():
            raise FileNotFoundError(
                f"$DIZZY_FEAT_PATH is not a file: {path}"
                + (" (it is a directory)" if path.is_dir() else "")
            )
        return path
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        found = sorted(directory.glob("*.feat.yaml"))
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise RuntimeError(
                f"{directory} holds {len(found)} feat files "
                f"({', '.join(p.name for p in found)}) — set $DIZZY_FEAT_PATH"
            )
    raise FileNotFoundError(f"no *.feat.yaml found from {here} upward — set $DIZZY_FEAT_PATH")


class FeatGraph:
    """An app's declared topology, with its generated classes resolved.

    Construct with :meth:`load`. Cheap to hold; every resolution is cached.
    """

    def __init__(
        self, feat_path: Path, raw: dict[str, Any], def_package: str = DEFAULT_DEF_PACKAGE
    ):
        self.feat_path = feat_path
        self.raw = raw
        self.def_package = def_package

    @classmethod
    def load(
        cls, feat_path: str | Path | None = None, def_package: str = DEFAULT_DEF_PACKAGE
    ) -> FeatGraph:
        import yaml

        path = Path(feat_path) if feat_path else find_feat()
        raw = yaml.safe_load(path.read_text()) or {}
        if not isinstance(raw, dict):
            raise RuntimeError(
                f"{path.name} is not a feat file: its top level is "
                f"{type(raw).__name__}, expected a mapping of sections"
            )
        graph = cls(path, raw, def_package)
        graph._check_shape()
        return graph

    def _check_shape(self) -> None:
        """Fail at load, naming the section — not later, deep in a stdlib call.

        A section written as a list or a bare string is legal YAML and a
        common slip; left alone, a list section half-works (``names()`` looks
        right, ``entry()`` blows up with AttributeError) and a string section
        is iterated one CHARACTER at a time.
        """
        for section in SECTIONS:
            value = self.raw.get(section)
            if value is None or isinstance(value, dict):
                continue
            hint = (
                " — a list of names is not a section; each entry needs a name: description mapping"
                if isinstance(value, list)
                else ""
            )
            raise RuntimeError(
                f"{self.feat_path.name}: section {section!r} is a "
                f"{type(value).__name__}, expected a mapping{hint}"
            )
        for section in SECTIONS:
            for name in self.raw.get(section) or {}:
                check_name(name, section, self.feat_path.name)

    # ── Declared names (no imports needed) ───────────────────────────────────

    def _section(self, section: str) -> dict[str, Any]:
        if section not in SECTIONS:
            raise KeyError(
                f"{section!r} is not a feat section — expected one of {', '.join(SECTIONS)}"
            )
        return self.raw.get(section) or {}

    def names(self, section: str) -> tuple[str, ...]:
        """The names the feat declares in *section*, in feat order."""
        return tuple(self._section(section))

    def entry(self, section: str, name: str) -> dict[str, Any]:
        """One declaration, normalized to a dict.

        A bare string is a description-only entry (how the feat spells most
        commands); a NULL value is a declared-but-unwritten entry, which is
        normal while drafting — it is present, just empty.
        """
        declared = self._section(section)
        if name not in declared:
            raise KeyError(f"{section}.{name} is not declared in {self.feat_path.name}")
        value = declared[name]
        if value is None:
            return {}
        if isinstance(value, str):
            return {"description": value}
        if isinstance(value, dict):
            return dict(value)
        raise RuntimeError(
            f"{self.feat_path.name}: {section}.{name} is a "
            f"{type(value).__name__}, expected a mapping or a description string"
        )

    @property
    def environment(self) -> tuple[str, ...]:
        """Environment field names — what a shell must re-hydrate per command.

        Derived, so adding an env shape to the feat needs no shell change.
        """
        return self.names("environment")

    @property
    def telemetry(self) -> tuple[str, ...]:
        """Telemetry sink names — the ports a shell may re-route as transport."""
        return self.names("telemetry")

    # ── Resolved classes (lazy per section) ──────────────────────────────────

    def _resolve(self, section: str) -> dict[str, type]:
        module_name = f"{self.def_package}.{_CLASS_SECTIONS[section]}"
        module = importlib.import_module(module_name)
        out: dict[str, type] = {}
        missing: list[str] = []
        for name in self.names(section):
            cls = getattr(module, camel_case(name), None)
            if isinstance(cls, type):
                out[name] = cls
            else:
                missing.append(f"{name} ({camel_case(name)})")
        if missing:
            raise RuntimeError(
                f"{self.feat_path.name} declares {section} that {module_name} "
                f"does not provide: {', '.join(missing)} — regenerate with "
                f"`dizzy generate definitions`"
            )
        return out

    @cached_property
    def commands(self) -> dict[str, type]:
        """Command name -> generated pydantic class."""
        return self._resolve("commands")

    @cached_property
    def events(self) -> dict[str, type]:
        """Event name -> generated pydantic class."""
        return self._resolve("events")

    def command_class(self, name: str) -> type:
        cls = self.commands.get(name)
        if cls is None:
            raise KeyError(f"unknown command {name!r} — not declared in {self.feat_path.name}")
        return cls

    def command_name(self, command: Any) -> str:
        """The feat name of a command INSTANCE (or class)."""
        cls = command if isinstance(command, type) else type(command)
        return snake_case(cls.__name__)

    def event_name(self, event: Any) -> str:
        cls = event if isinstance(event, type) else type(event)
        return snake_case(cls.__name__)

    # ── Validation ───────────────────────────────────────────────────────────

    def validate_registered(self, registered: dict[str, set[str]]) -> None:
        """Assert an app's registered elements are exactly what the feat declares.

        *registered* maps a topology section to the names the app actually
        wired. Replaces the hand-maintained ``_REGISTERED`` literal: the feat
        side is read, so only the app's own wiring must be reported.
        """
        problems: list[str] = []
        for section, wired in registered.items():
            # A typo'd section key would otherwise compare against an empty
            # set: silently PASSING when nothing is wired, and blaming the app
            # for "not wiring" every real element when something is.
            declared = set(self.names(section))
            if declared != set(wired):
                problems.append(
                    f"{section}: not wired={sorted(declared - set(wired))} "
                    f"not in feat={sorted(set(wired) - declared)}"
                )
        if problems:
            raise RuntimeError(
                f"wiring/feat mismatch against {self.feat_path.name}: " + "; ".join(problems)
            )


_graphs: dict[tuple[Path, str], FeatGraph] = {}


def graph(feat_path: str | Path | None = None, def_package: str = DEFAULT_DEF_PACKAGE) -> FeatGraph:
    """Process-wide FeatGraph cache — a worker parses its feat once.

    Keyed on the RESOLVED path, not on nothing: an earlier version cached a
    single graph, so once any caller had built one, a later `$DIZZY_FEAT_PATH`
    (or a different cwd, since discovery walks up) silently handed back the
    first caller's feat. Re-resolving per call is two env reads; parsing is
    what the cache is for.
    """
    path = Path(feat_path) if feat_path is not None else find_feat()
    key = (path.resolve(), def_package)
    if key not in _graphs:
        _graphs[key] = FeatGraph.load(path, def_package)
    return _graphs[key]


def reset_graph() -> None:
    """Drop the cache — for tests that rewrite a feat file in place."""
    _graphs.clear()
