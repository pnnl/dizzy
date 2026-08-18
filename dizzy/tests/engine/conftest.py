"""Fixtures for the engine tests.

``FeatGraph`` resolves declared names against a GENERATED definitions package.
Rather than depend on a generation run, these tests synthesise a tiny package
with the same shape (``<pkg>.commands`` / ``.events`` holding pydantic-ish
classes) and point ``def_package`` at it — so the naming convention is under
test, not the generator.
"""

from __future__ import annotations

import sys
import textwrap
from collections.abc import Sequence
from pathlib import Path

import pytest

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def def_package(tmp_path, monkeypatch):
    """Build an importable stand-in for a generated definitions package.

    Returns a callable: ``def_package(commands=[...], events=[...]) -> str``
    (the importable package name to pass as ``def_package``).
    """
    counter = {"n": 0}

    def build(commands: Sequence[str] = (), events: Sequence[str] = ()) -> str:
        counter["n"] += 1
        name = f"fakegen{counter['n']}"
        root = tmp_path / name
        (root / "pydantic").mkdir(parents=True)
        (root / "__init__.py").write_text("")
        (root / "pydantic" / "__init__.py").write_text("")
        for module, class_names in (("commands", commands), ("events", events)):
            body = "\n".join(
                f"class {c}:\n    pass\n" for c in class_names) or "pass\n"
            (root / "pydantic" / f"{module}.py").write_text(
                textwrap.dedent(body))
        monkeypatch.syspath_prepend(str(tmp_path))
        for mod in list(sys.modules):
            if mod.startswith(name):
                del sys.modules[mod]
        return f"{name}.pydantic"

    return build


@pytest.fixture
def write_feat(tmp_path):
    """Write a feat file and return its path."""
    def write(body: str, name: str = "app.feat.yaml") -> Path:
        path = tmp_path / name
        path.write_text(body)
        return path
    return write


@pytest.fixture(autouse=True)
def _no_ambient_feat(monkeypatch):
    """Discovery walks UP from the cwd, and this repo has feat files at its
    root — an unset variable would otherwise let a test find one by accident.
    """
    monkeypatch.delenv("DIZZY_FEAT_PATH", raising=False)
    monkeypatch.delenv("DIZZY_HOST_APP", raising=False)
    from dizzy.engine.registry import reset_graph
    reset_graph()
    yield
    reset_graph()
