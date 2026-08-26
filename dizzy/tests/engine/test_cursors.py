"""Cursor stores: a checkpoint that may be lost, never a second source of truth.

topology.yaml calls a cursor "durable ... but disposable: losing one must be
safe, not merely recoverable". A prose claim like that decays; the tests below
are written so that any change making a cursor load-bearing fails one of them.
The load-bearing one is :func:`test_deleting_the_cursor_replays_to_identical_state`
— everything else guards a way of arriving at the same mistake.

The "projection" here is a deliberately dull word-count fold. Nothing in the
cursor module knows about events, so the fold exists only to give "the same
state" something to mean.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest
from dizzy.engine.cursors import (
    START,
    CursorStore,
    MemoryCursorStore,
    SqliteCursorStore,
    open_cursor_store,
)
from dizzy.engine.store import EventStore
from pydantic import BaseModel


class RecipeDefined(BaseModel):
    recipe_id: str
    servings: int = 1


EVENT_CLASSES = {"recipe_defined": RecipeDefined}


@pytest.fixture(params=["memory", "sqlite"])
def cursors(request, tmp_path):
    """Both shipped kinds behind one fixture.

    Every test that runs on this fixture is a test that the kinds are
    interchangeable — which is the claim topology.yaml makes when it offers
    `memory` for tests and `sqlite` for a real boot.
    """
    if request.param == "memory":
        store = MemoryCursorStore()
    else:
        store = SqliteCursorStore(tmp_path / "cursors.db")
    yield store
    store.close()


@pytest.fixture
def events(tmp_path):
    store = EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)
    for i in range(12):
        store.append(RecipeDefined(recipe_id=f"r{i}", servings=i))
    return store


def fold(events: EventStore, cursors: CursorStore, group: str, model: dict) -> dict:
    """Tail the log into *model*, committing before saving. See the module
    docstring on :mod:`dizzy.engine.cursors`: that order is the crash rule."""
    for envelope in events.iterate_from(cursors.load(group)):
        model[envelope.payload["recipe_id"]] = envelope.payload["servings"]
        model["_folds"] = model.get("_folds", 0) + 1
        cursors.save(group, envelope.position)
    return model


# ── The contract: a cursor may be lost ──────────────────────────────────────


def test_deleting_the_cursor_replays_to_identical_state(events, cursors):
    """Delete the cursor, replay from zero, compare. This is the property that
    keeps a cursor a performance checkpoint and not a second source of truth:
    the log alone determines the state, and the cursor only determines how much
    of it we re-read."""
    with_cursor = fold(events, cursors, "g", {})

    cursors.rewind("g", START)  # the cursor is gone; the log is not
    from_zero = fold(events, MemoryCursorStore(), "g", {})

    assert from_zero == with_cursor


def test_a_lost_cursor_costs_work_not_correctness(events, cursors):
    """The same state, reached by folding strictly more events. Naming the cost
    is the point — if a future change made the replay cheap by remembering
    something outside the log, this assertion is where it would show up."""
    fold(events, cursors, "g", {})
    resumed = fold(events, cursors, "g", {})  # nothing new: cursor is at the tail
    replayed = fold(events, MemoryCursorStore(), "g", {})

    assert resumed.get("_folds") is None  # the resumed pass folded nothing
    assert replayed["_folds"] == 12
    assert {k: v for k, v in replayed.items() if k != "_folds"} == {f"r{i}": i for i in range(12)}


def test_a_partial_cursor_and_a_full_replay_agree(events, cursors):
    """Interrupt the fold anywhere and resume: same answer as never stopping.
    Parametrised over every cut point, because an off-by-one in save/resume
    hides at exactly one of them."""
    reference = fold(events, MemoryCursorStore(), "g", {})
    del reference["_folds"]

    for cut in range(13):
        store = MemoryCursorStore()
        model: dict = {}
        for n, envelope in enumerate(events.iterate_from(START)):
            if n == cut:
                break
            model[envelope.payload["recipe_id"]] = envelope.payload["servings"]
            store.save("g", envelope.position)
        fold(events, store, "g", model)
        model.pop("_folds", None)  # a cut at the tail resumes zero events
        assert model == reference, f"resume after {cut} events diverged"


def test_an_unknown_group_starts_at_the_beginning(cursors):
    """Not an error, not None. A missing cursor is the instruction 'replay',
    and a consumer that has to branch on absence will branch wrongly."""
    assert cursors.load("never-seen") == START
    assert cursors.groups() == {}


# ── Groups are independent ──────────────────────────────────────────────────


def test_two_groups_hold_independent_positions(events, cursors):
    """topology.yaml: two groups is a SEMANTIC decision — they advance
    independently. One store, no interference."""
    fast: dict = {}
    slow: dict = {}
    for n, envelope in enumerate(events.iterate_from(START)):
        fast[envelope.payload["recipe_id"]] = envelope.payload["servings"]
        cursors.save("fast", envelope.position)
        if n < 4:
            slow[envelope.payload["recipe_id"]] = envelope.payload["servings"]
            cursors.save("slow", envelope.position)

    assert cursors.load("fast") > cursors.load("slow")
    assert len(slow) == 4

    fold(events, cursors, "slow", slow)
    del slow["_folds"]
    assert slow == fast  # the laggard catches up to the same state


def test_advancing_one_group_never_moves_another(cursors):
    cursors.save("a", 10)
    cursors.save("b", 3)
    cursors.save("a", 99)
    assert cursors.load("b") == 3
    assert cursors.groups() == {"a": 99, "b": 3}


# ── Stale rows: a group the topology no longer declares ─────────────────────


def test_a_cursor_for_a_vanished_group_is_inert(events, cursors):
    """A renamed group leaves its old row behind. Nothing loads a group the
    topology does not declare, so the row must not be able to affect a live
    one — and must not be deleted on a guess either, because during a rolling
    deploy an 'unrecognised' name is often a group this host has not learned
    about yet."""
    cursors.save("removed_last_release", 7)
    fold(events, cursors, "live", {})

    assert cursors.load("removed_last_release") == 7  # untouched
    assert cursors.load("live") > 0
    assert "removed_last_release" in cursors.groups()


def test_prune_drops_exactly_the_groups_the_topology_no_longer_names(cursors):
    cursors.save("live", 5)
    cursors.save("also_live", 6)
    cursors.save("stale", 7)

    assert cursors.prune({"live", "also_live"}) == ("stale",)
    assert set(cursors.groups()) == {"live", "also_live"}
    assert cursors.prune({"live", "also_live"}) == ()  # idempotent


def test_pruning_a_live_group_only_costs_a_replay(events, cursors):
    """Even the destructive operation is bounded by disposability: prune the
    wrong name and the group re-folds, it does not lose state."""
    model = fold(events, cursors, "g", {})
    assert cursors.prune(set()) == ("g",)

    replayed = fold(events, cursors, "g", {})
    assert replayed == model


# ── Crash safety ────────────────────────────────────────────────────────────


def test_a_crash_between_commit_and_save_redelivers_and_converges(events, cursors):
    """The store cannot enforce commit-then-save, so this asserts the outcome
    the rule buys: work committed but not yet checkpointed is re-delivered, and
    a confluent fold lands on the same state. At-least-once, never at-most."""
    model: dict = {}
    delivered = []
    for n, envelope in enumerate(events.iterate_from(START)):
        model[envelope.payload["recipe_id"]] = envelope.payload["servings"]
        delivered.append(envelope.id)
        if n == 5:
            break  # crash HERE: committed, cursor not yet saved
        cursors.save("g", envelope.position)

    resumed = [e.id for e in events.iterate_from(cursors.load("g"))]
    assert resumed[0] == delivered[5], "the uncommitted-cursor event must come back"

    fold(events, cursors, "g", model)
    del model["_folds"]
    assert model == {f"r{i}": i for i in range(12)}


def test_the_position_never_advances_past_committed_work(events, cursors):
    """The invariant, checked continuously rather than at the end: at every
    moment the saved position names an event whose effect is already in the
    model."""
    model: dict = {}
    for envelope in events.iterate_from(START):
        assert cursors.load("g") < envelope.position  # not yet claimed
        model[envelope.payload["recipe_id"]] = envelope.payload["servings"]
        cursors.save("g", envelope.position)
        assert len(model) == len(
            [e for e in events.iterate_from(START) if e.position <= cursors.load("g")]
        )


def test_save_is_monotone_so_a_late_stale_save_cannot_rewind(cursors):
    """A save trailing committed work means a delayed one can carry a stale
    position. Letting it win would re-deliver work nobody asked to re-do, and
    a watermark that can move backwards on its own is not a watermark. What it
    does NOT buy is a group whose workers split the stream between them — see
    `save`, which says so."""
    cursors.save("g", 40)
    assert cursors.save("g", 10) == 40  # the straggler is ignored, loudly in the return
    assert cursors.load("g") == 40
    assert cursors.save("g", 41) == 41


def test_rewind_is_the_only_way_backwards(cursors):
    """Deliberate replay exists, but it is a different verb from the one a
    worker calls in its loop — an off-by-one must not be able to reach it."""
    cursors.save("g", 40)
    cursors.rewind("g", 10)
    assert cursors.load("g") == 10
    cursors.rewind("g")
    assert cursors.load("g") == START


def test_a_position_before_the_start_of_the_log_is_rejected(cursors):
    with pytest.raises(ValueError, match="watermark"):
        cursors.save("g", -1)
    with pytest.raises(ValueError, match="group name"):
        cursors.save("", 1)


# ── Durability and multi-process access (sqlite only) ───────────────────────


def test_sqlite_cursors_survive_the_process_that_wrote_them(tmp_path, events):
    path = tmp_path / "cursors.db"
    first = SqliteCursorStore(path)
    fold(events, first, "g", {})
    saved = first.load("g")
    first.close()

    reopened = SqliteCursorStore(path)
    assert reopened.load("g") == saved
    assert reopened.recovered == ()
    reopened.close()


def test_a_corrupt_position_rewinds_rather_than_guessing_forward(tmp_path):
    """A row this code could not have written — an interrupted migration, a
    hand edit. There is one safe repair, and disposability is what makes it
    safe: guessing forward skips work nothing can recover, guessing backward
    costs a replay."""
    path = tmp_path / "cursors.db"
    store = SqliteCursorStore(path)
    store.save("good", 5)
    store.save("bad", 5)
    store._db.execute("UPDATE cursors SET position=-3 WHERE group_name='bad'")
    store._db.commit()
    store.close()

    reopened = SqliteCursorStore(path)
    assert reopened.recovered == ("bad",)
    assert reopened.load("bad") == START
    assert reopened.load("good") == 5
    reopened.close()


def test_a_text_position_rewinds_like_any_other_unusable_row(tmp_path):
    """SQLite's INTEGER is an affinity, not a constraint, so the hand edit the
    docstring advertises can leave TEXT in the column — and TEXT sorts above
    every integer, so a value-only check passes it through and then every
    method raises on the int(), taking `groups` and `prune` down with it."""
    path = tmp_path / "cursors.db"
    store = SqliteCursorStore(path)
    store.save("good", 5)
    store.save("bad", 5)
    store._db.execute("UPDATE cursors SET position='garbage' WHERE group_name='bad'")
    store._db.commit()
    store.close()

    reopened = SqliteCursorStore(path)
    assert reopened.recovered == ("bad",)
    assert reopened.groups() == {"bad": START, "good": 5}
    assert reopened.save("bad", 3) == 3  # usable again, not stuck above every int
    reopened.close()


def test_deleting_the_sqlite_file_replays_to_identical_state(tmp_path, events):
    """The disposability claim on the DURABLE kind, done the way an operator
    would: stop the process, remove the file, boot again. The unlink is with
    the store closed, because a file another connection still holds is a POSIX
    question and not a cursor one."""
    path = tmp_path / "cursors.db"
    store = SqliteCursorStore(path)
    with_cursor = fold(events, store, "g", {})
    store.close()

    path.unlink()
    for sidecar in (path.with_name(path.name + "-wal"), path.with_name(path.name + "-shm")):
        sidecar.unlink(missing_ok=True)

    replaced = SqliteCursorStore(path)
    assert replaced.load("g") == START
    from_zero = fold(events, replaced, "g", {})
    replaced.close()

    assert from_zero == with_cursor


WRITER = """
import sys
from dizzy.engine.cursors import SqliteCursorStore

path, group, start, count = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4])
store = SqliteCursorStore(path)
for i in range(count):
    store.save(group, start + i)
store.close()
"""


def test_two_processes_sharing_one_cursor_file_converge_on_the_maximum(tmp_path):
    """WAL plus busy_timeout plus a single upserting statement: no reader
    blocks the writer, no writer raises, and the monotone max is decided by
    SQLite under the row lock rather than by a read-then-write in Python that
    two processes could interleave."""
    path = tmp_path / "cursors.db"
    script = tmp_path / "writer.py"
    script.write_text(textwrap.dedent(WRITER))

    procs = [
        subprocess.Popen(
            [sys.executable, str(script), str(path), "shared", str(start), "2000"],
            stderr=subprocess.PIPE,
        )
        for start in (0, 5_000)
    ]
    local = SqliteCursorStore(path)
    for i in range(2_000):
        local.save("shared", i)
        local.load("shared")

    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, err.decode()

    # 5_000..6_999 from the second writer is the high water; the thousands of
    # lower saves racing it must have been absorbed, not applied.
    assert local.load("shared") == 6_999
    local.close()


def test_a_second_process_never_drags_a_cursor_backwards(tmp_path):
    path = tmp_path / "cursors.db"
    script = tmp_path / "writer.py"
    script.write_text(textwrap.dedent(WRITER))

    ahead = SqliteCursorStore(path)
    ahead.save("shared", 10_000)

    proc = subprocess.run(
        [sys.executable, str(script), str(path), "shared", "0", "50"],
        capture_output=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert ahead.load("shared") == 10_000
    ahead.close()


# ── The factory, and the kind that is not implemented ───────────────────────


def test_open_cursor_store_builds_the_kinds_topology_names(tmp_path):
    assert isinstance(open_cursor_store("memory"), MemoryCursorStore)
    sqlite = open_cursor_store("sqlite", str(tmp_path / "c.db"))
    assert isinstance(sqlite, SqliteCursorStore)
    sqlite.close()


def test_postgres_is_declared_but_deliberately_absent():
    """topology.yaml names three kinds; this project depends on pyyaml and
    pydantic, and no postgres driver is among its extras. Saying so beats a
    half-store that fails at the first connection."""
    with pytest.raises(NotImplementedError, match="not.*implemented|not a dependency"):
        open_cursor_store("postgres", "postgresql://localhost/x")


def test_an_unknown_kind_names_the_ones_that_exist():
    with pytest.raises(ValueError, match="memory, sqlite, postgres"):
        open_cursor_store("etcd")


def test_a_sqlite_url_names_the_same_file_a_bare_path_does(tmp_path, monkeypatch):
    """topology.yaml calls the field "Connection string or path", so a url is
    a declared-legal value. Handed to Path verbatim it makes a directory
    called `sqlite:` under the cwd, and a boot from elsewhere then replays
    from zero while looking healthy."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()

    url = open_cursor_store("sqlite", "sqlite:///data/cursors.db")
    url.save("g", 4)
    url.close()
    assert not (tmp_path / "sqlite:").exists()

    plain = open_cursor_store("sqlite", "data/cursors.db")
    assert plain.load("g") == 4
    plain.close()

    absolute = SqliteCursorStore(f"sqlite:///{tmp_path / 'abs.db'}")
    assert absolute.path == tmp_path / "abs.db"
    absolute.close()


def test_a_url_for_another_engine_is_refused_rather_than_made_into_a_directory():
    with pytest.raises(ValueError, match="not a sqlite location"):
        open_cursor_store("sqlite", "postgresql://localhost/cursors")


def test_both_kinds_satisfy_the_protocol(cursors):
    """`runtime_checkable` compares method NAMES only, so the isinstance is a
    smoke test and the calls below are the contract a third implementation is
    graded on: START for an unknown group, a monotone int back from save, a
    dict from groups, None from rewind, a tuple of what prune dropped."""
    assert isinstance(cursors, CursorStore)

    assert cursors.load("fresh") == START
    assert cursors.save("fresh", 7) == 7
    assert cursors.save("fresh", 2) == 7
    assert cursors.groups() == {"fresh": 7}
    assert cursors.rewind("fresh", 1) is None
    assert cursors.load("fresh") == 1
    assert cursors.prune(()) == ("fresh",)
    assert cursors.groups() == {}
