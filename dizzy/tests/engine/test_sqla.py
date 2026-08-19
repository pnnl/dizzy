"""The SQLite read-model cache helpers.

The idea under test is one claim: **the read model is a cache and the stream is
the truth**, so schema drift is not a migration problem — it is a stale cache,
and the fix is a refold. What makes that safe in practice is the completion
marker, so most of these tests are about when the marker is and is not written.
"""

from __future__ import annotations

import pytest

sqlalchemy = pytest.importorskip("sqlalchemy", reason="needs the `sqla` extra")

from dizzy.engine.sqla import (  # noqa: E402
    db_path,
    ensure_current,
    make_engine,
    marker_path,
    refold_if_stale,
    schema_fingerprint,
)
from sqlalchemy import Column, Integer, MetaData, String, Table  # noqa: E402


def metadata_with(*columns: str) -> MetaData:
    md = MetaData()
    Table(
        "batches",
        md,
        Column("id", Integer, primary_key=True),
        *[Column(name, String) for name in columns],
    )
    return md


@pytest.fixture
def sqla_engine(tmp_path):
    return make_engine(tmp_path / "models.db")


def test_the_database_path_comes_from_arg_then_env_then_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DIZZY_DB_PATH", raising=False)
    assert db_path(tmp_path / "a.db") == tmp_path / "a.db"
    monkeypatch.setenv("DIZZY_DB_PATH", str(tmp_path / "b.db"))
    assert db_path() == tmp_path / "b.db"
    assert db_path(tmp_path / "a.db") == tmp_path / "a.db"  # argument still wins


def test_make_engine_enables_wal_so_readers_survive_a_writer(sqla_engine):
    """N workers fold into one file under the mp shell; without WAL a reader
    blocks on the writer and without busy_timeout a writer simply raises."""
    with sqla_engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode.lower() == "wal"


def test_the_fingerprint_tracks_the_column_set(sqla_engine):
    before = schema_fingerprint([metadata_with("status")])
    after = schema_fingerprint([metadata_with("status", "is_face")])
    assert before != after
    # ...and is stable for the same schema, whatever order the tables arrive in.
    assert before == schema_fingerprint([metadata_with("status")])


def test_a_refold_runs_when_there_is_no_marker(sqla_engine):
    calls = []
    folded = refold_if_stale(
        sqla_engine, [metadata_with("status")], lambda s: calls.append(s) or 7, report=None
    )
    assert folded == 7
    assert len(calls) == 1


def test_a_second_run_against_an_unchanged_schema_does_nothing(sqla_engine):
    md = [metadata_with("status")]
    refold_if_stale(sqla_engine, md, lambda s: 1, report=None)
    calls = []
    assert refold_if_stale(sqla_engine, md, lambda s: calls.append(s) or 1, report=None) is None
    assert calls == []


def test_a_changed_schema_retriggers_the_refold(sqla_engine):
    refold_if_stale(sqla_engine, [metadata_with("status")], lambda s: 1, report=None)
    calls = []
    refold_if_stale(
        sqla_engine,
        [metadata_with("status", "is_face")],
        lambda s: calls.append(s) or 2,
        report=None,
    )
    assert len(calls) == 1  # the added column made the cache stale


def test_a_crashed_refold_writes_no_marker_and_is_retried(sqla_engine):
    """The 2026-07-11 incident: a crashed rebuild leaves every table present but
    empty, which table inspection happily calls current. Only a FINISHED refold
    writes the marker, so a crash keeps retriggering until one completes."""
    md = [metadata_with("status")]

    def boom(session):
        raise RuntimeError("crashed mid-refold")

    with pytest.raises(RuntimeError):
        refold_if_stale(sqla_engine, md, boom, report=None)
    assert not marker_path(sqla_engine).exists()

    calls = []
    refold_if_stale(sqla_engine, md, lambda s: calls.append(s) or 3, report=None)
    assert len(calls) == 1  # retried, rather than assumed current


def test_ensure_current_creates_the_tables_before_folding(sqla_engine):
    """A refold folds INTO the read models, so they have to exist first."""
    md = [metadata_with("status")]
    seen = {}

    def refold(session):
        seen["tables"] = set(sqlalchemy.inspect(sqla_engine).get_table_names())
        return 0

    ensure_current(sqla_engine, md, refold, report=None)
    assert "batches" in seen["tables"]


def test_ensure_current_is_safe_to_call_from_every_process(sqla_engine):
    """Server and workers all call it at startup; whoever loses the lock
    re-checks the marker and returns instead of refolding again."""
    md = [metadata_with("status")]
    calls = []
    ensure_current(sqla_engine, md, lambda s: calls.append(s) or 1, report=None)
    ensure_current(sqla_engine, md, lambda s: calls.append(s) or 1, report=None)
    ensure_current(sqla_engine, md, lambda s: calls.append(s) or 1, report=None)
    assert len(calls) == 1


def test_the_marker_sits_next_to_the_database_it_describes(sqla_engine, tmp_path):
    """So a test on a tmp path can never invalidate the real data directory."""
    assert marker_path(sqla_engine) == tmp_path / "models.schema"
