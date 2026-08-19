"""The control loop: two queues, and the ordering rule between them.

What matters here is not that handlers get called, but WHEN: projections fold
and the read model commits before policies dispatch, and a policy's command
never runs inline — it leaves through the external queue and becomes the next
unit of work. Those two properties are what the scheduling shells rely on, so
they are asserted directly rather than inferred from an end-to-end result.
"""

from __future__ import annotations

import pytest
from dizzy.engine.loop import Engine
from dizzy.engine.ports import NullOtel
from dizzy.engine.store import EventStore
from pydantic import BaseModel


class DefineRecipe(BaseModel):
    recipe_id: str


class StartBatch(BaseModel):
    recipe_id: str


class RecipeDefined(BaseModel):
    recipe_id: str


class BatchOpened(BaseModel):
    recipe_id: str


EVENT_CLASSES = {"recipe_defined": RecipeDefined, "batch_opened": BatchOpened}


class Queue:
    """Stand-in for the shell's command queue."""

    def __init__(self):
        self.items = []

    def put(self, command, origin="policy"):
        self.items.append(command)

    def qsize(self):
        return len(self.items)


@pytest.fixture
def engine(tmp_path):
    store = EventStore(path=tmp_path / "events.db", event_classes=EVENT_CLASSES)
    return Engine(command_queue=Queue(), store=store)


def test_a_command_runs_its_registered_procedure(engine):
    seen = []
    engine.register_procedure(DefineRecipe, seen.append, name="define_recipe")
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert [c.recipe_id for c in seen] == ["r1"]


def test_an_unregistered_command_says_the_wiring_missed_it(engine):
    with pytest.raises(KeyError, match="DefineRecipe"):
        engine.run_command(DefineRecipe(recipe_id="r1"))


def test_an_emitted_event_is_appended_before_it_is_folded(engine):
    """The store is the truth: a projection must never see an event the stream
    does not already hold."""
    lengths = []
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id=c.recipe_id))
    )
    engine.register_projection(RecipeDefined, lambda e, at: lengths.append(len(engine.store)))
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert lengths == [1]


def test_projections_fold_and_commit_before_policies_dispatch(engine):
    """The ordering rule. The commit is what makes fold-then-enqueue real
    across processes — another worker may claim the dispatched command
    immediately, and it sees only committed state."""
    order = []
    engine.commit = lambda: order.append("commit")
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id=c.recipe_id))
    )
    engine.register_projection(RecipeDefined, lambda e, at: order.append("fold"))
    engine.register_policy(RecipeDefined, lambda e: order.append("policy"))
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert order == ["fold", "commit", "policy"]


def test_a_dispatched_command_leaves_through_the_queue_and_does_not_run_inline(engine):
    """The reactivity loop crosses the process boundary here: the shell owns
    the command phase, so the cascade stops at the queue."""
    ran = []
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id=c.recipe_id))
    )
    engine.register_procedure(StartBatch, ran.append)
    engine.register_policy(
        RecipeDefined, lambda e: engine.dispatch_command(StartBatch(recipe_id=e.recipe_id))
    )
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert ran == []  # NOT run inline
    assert [type(c).__name__ for c in engine.command_queue.items] == ["StartBatch"]


def test_the_event_queue_drains_to_quiescence_within_one_command(engine):
    """A procedure's own cascade is this unit of work; only commands leave."""
    folded = []
    engine.register_procedure(
        DefineRecipe,
        lambda c: [
            engine.emit_event(RecipeDefined(recipe_id="r1")),
            engine.emit_event(BatchOpened(recipe_id="r1")),
        ],
    )
    engine.register_projection(RecipeDefined, lambda e, at: folded.append("recipe"))
    engine.register_projection(BatchOpened, lambda e, at: folded.append("batch"))
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert folded == ["recipe", "batch"]
    assert len(engine.store) == 2


def test_a_projection_sees_the_envelopes_ingested_at(engine):
    seen = []
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id="r1"))
    )
    engine.register_projection(RecipeDefined, lambda e, at: seen.append(at))
    engine.run_command(DefineRecipe(recipe_id="r1"))
    (envelope,) = list(engine.store.iterate())
    assert seen == [envelope.ingested_at]


def test_current_event_names_the_cause_while_policies_run(engine):
    seen = []
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id="r1"))
    )
    engine.register_policy(RecipeDefined, lambda e: seen.append(engine.current_event))
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert [type(e).__name__ for e in seen] == ["RecipeDefined"]
    assert engine.current_event is None  # cleared afterwards


def test_several_projections_on_one_event_all_fold_it(engine):
    folded = []
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id="r1"))
    )
    engine.register_projection(RecipeDefined, lambda e, at: folded.append("a"), name="a")
    engine.register_projection(RecipeDefined, lambda e, at: folded.append("b"), name="b")
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert folded == ["a", "b"]


def test_the_observer_sees_every_event_by_its_feat_name(engine):
    seen = []
    engine.observer = lambda name, event: seen.append(name)
    engine.register_procedure(
        DefineRecipe, lambda c: engine.emit_event(RecipeDefined(recipe_id="r1"))
    )
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert seen == ["recipe_defined"]


def test_discard_pending_events_stops_a_failure_leaking_into_the_next_command(engine):
    """A long-lived process reuses one engine; a half-run command must not
    hand its un-appended events to the next."""
    engine.register_procedure(
        DefineRecipe,
        lambda c: (
            engine.emit_event(RecipeDefined(recipe_id="r1")),
            (_ for _ in ()).throw(ValueError("nope")),
        ),
    )
    with pytest.raises(ValueError):
        engine.run_command(DefineRecipe(recipe_id="r1"))
    engine.discard_pending_events()
    assert len(engine.store) == 0
    engine.register_procedure(DefineRecipe, lambda c: None)
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert len(engine.store) == 0  # the failed command's event never appears


# ── Self-observation ────────────────────────────────────────────────────────


def test_the_bus_gets_a_record_per_element_and_per_appended_event(engine):
    records = []
    engine.bus = records.append
    engine.register_procedure(
        DefineRecipe,
        lambda c: engine.emit_event(RecipeDefined(recipe_id="r1")),
        name="define_recipe",
    )
    engine.register_projection(RecipeDefined, lambda e, at: None, name="recipe_book")
    engine.register_policy(RecipeDefined, lambda e: None, name="open_batch")
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert [(r["kind"], r["name"]) for r in records] == [
        ("procedure", "define_recipe"),
        ("event", "recipe_defined"),
        ("projection", "recipe_book"),
        ("policy", "open_batch"),
    ]
    assert all(r["outcome"] in {"ok", "appended"} for r in records)


def test_a_failing_element_is_recorded_then_re_raised(engine):
    """The engine observes; it does not swallow."""
    records = []
    engine.bus = records.append

    def boom(command):
        raise ValueError("nope")

    engine.register_procedure(DefineRecipe, boom, name="define_recipe")
    with pytest.raises(ValueError, match="nope"):
        engine.run_command(DefineRecipe(recipe_id="r1"))
    assert records[0]["outcome"] == "error"
    assert records[0]["detail"] == "ValueError: nope"


def test_without_a_bus_the_engine_does_not_trace_at_all(engine):
    """Telemetry is opt-in; the un-instrumented path must stay a plain call."""

    class Explode:
        def __getattr__(self, name):
            raise AssertionError(f"engine reached for otel.{name} with no bus")

    engine.otel = Explode()
    ran = []
    engine.register_procedure(DefineRecipe, ran.append)
    engine.run_command(DefineRecipe(recipe_id="r1"))  # bus is None
    assert len(ran) == 1


def test_the_null_otel_provider_makes_tracing_a_no_op(engine):
    """A host with tracing switched off must still run — no OTel SDK needed."""
    records = []
    engine.bus = records.append
    engine.register_procedure(DefineRecipe, lambda c: None, name="define_recipe")
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert records[0]["outcome"] == "ok"
    assert "trace_id" not in records[0]  # null provider yields no id


def test_a_host_otel_provider_is_used_for_spans_and_metrics(engine):
    """The engine reaches tracing through the injected provider, never an
    imported module — that is what lets a shell run someone else's app."""
    spans, measurements = [], []

    class Span:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    class Tracer:
        def start_as_current_span(self, name, **kw):
            spans.append(name)
            return Span()

    class Histogram:
        def record(self, value, attrs):
            measurements.append(attrs)

    class Meter:
        def create_histogram(self, *a, **k):
            return Histogram()

    class Otel:
        def tracer(self):
            return Tracer()

        def meter(self):
            return Meter()

        def trace_id_hex(self):
            return "abc123"

    engine.otel = Otel()
    engine.bus = lambda r: None
    engine.register_procedure(DefineRecipe, lambda c: None, name="define_recipe")
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert spans == ["procedure define_recipe"]
    assert measurements == [
        {"dizzy.kind": "procedure", "dizzy.name": "define_recipe", "dizzy.outcome": "ok"}
    ]


def test_the_trace_id_is_stamped_onto_bus_records_when_there_is_one(engine):
    records = []

    class Otel(NullOtel):
        def trace_id_hex(self):
            return "deadbeef"

    engine.otel = Otel()
    engine.bus = records.append
    engine.register_procedure(DefineRecipe, lambda c: None, name="define_recipe")
    engine.run_command(DefineRecipe(recipe_id="r1"))
    assert records[0]["trace_id"] == "deadbeef"
