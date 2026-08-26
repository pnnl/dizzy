# Deployment topology — design notes

!!! warning "Experimental"
    This describes work on the `engine-topology` branch. Nothing here ships, the
    schema is a first draft, and the open questions at the bottom are open. It is
    deliberately not in the site nav.

A feature-file says what a system *is*. It does not say where any of it runs, and
it should not — which elements share a process is a property of a deployment, not
of a design. Today that property is neither: it is baked into hand-written
wiring, so changing it is a code change and testing it is impossible.

The goal is one sentence: **the same feature-file, deployed at different
topologies per environment, with no code change.**

## The unit is a consumer group

A **consumer group** is a set of elements sharing one cursor over the log. That
is what `topology.yaml` names.

The distinction that matters, and the one most likely to be got wrong:

- **Worker count** is a *scaling* decision. Two workers on one group is
  operations, and safe to change without re-reasoning about correctness.
- **Group count** is a *semantic* decision. Two groups advance independently and
  can observe each other's effects out of order.

So groups are declared in a reviewed artifact and validated against the
feature-file at generation time. Worker counts are a number an operator turns.

## Two consumers, two pressures

The design has to serve both of these, and they are not the same shape.

| | **Isolation** (labthing) | **Fan-out** (DAG processing) |
|---|---|---|
| Shape | ~124 elements, one process, one global lock | 1 command → 10 → 100 → 1000 artifacts |
| The pain | A 40s paid model call freezes every write | Organising workers; finding the one failure in a thousand |
| Wants from topology | Split slow paid work off the lock | Wide worker pools, backpressure, per-unit error tracking |
| Cost sensitivity | Process count — self-hosted, ~50-100MB each | Throughput; process count is elastic |
| Must not lose | A 429-test suite that finishes in ~17s in-process | Any unit of work, ever |

Both are expressible in the current draft, which is the main evidence the schema
is not just labthing's problem in YAML. Both are shown here as first written, and
rule 6 below costs each of them an edit — said there rather than quietly fixed
here, because what the edit costs is the argument:

```yaml title="isolation"
groups:
  - name: fast          # holds the lock; must stay quick
    include: [{kind: projections}, {kind: policies}]
  - name: paid          # 40s turns block nothing else
    include: [{kind: procedures, names: ["*_completion", "summarize_*"]}]
    workers: 4
    retry: {max_attempts: 3, backoff: exponential, dead_letter: true}
  - name: default
    catch_all: true
```

```yaml title="fan-out"
groups:
  - name: expand        # the 10-to-100-to-1000 tier
    include: [{kind: procedures, names: ["expand_*", "derive_*"]}]
    workers: 32
    max_in_flight: 64
  - name: ledger        # one model's projections, never split
    include: [{model: artifact_ledger}, {kind: policies}]
  - name: default
    catch_all: true
```

Both examples scale a group of procedures, and `dizzy generate wiring` accepts that as
long as none of those procedures declares a `queries:` — see rule 3, which asks that same
question of every element a scaled group holds, policies included. The paid completions
and the `expand_*` tier both read nothing, so both scale as written. Concurrency means
both knobs: one worker holding 64 units in flight runs 64 units at once exactly as 64
workers do, so the gate reads `max(workers, max_in_flight)` and names whichever field the
author wrote.

Two schema decisions came directly out of writing those:

**Selectors, not enumeration.** 124 elements must not mean 124 lines. Selectors
match by kind, by name glob, or by model — and `{model: artifact_ledger}` is the
convenient *and* correct way to satisfy the rule that one model's projections may
never be split.

**`catch_all`.** The first draft made "an element claimed by two groups" a
generation error, which is right, but it also made a trailing `default` group
illegal — and without one, adding an element to the feature-file either leaves it
silently unplaced or forces every topology to be edited in lockstep with the
design. Exactly one group may be `catch_all`; overlap between *explicit*
selectors is still an error.

## What generation does

`dizzy generate wiring` already turns the feature-file into per-element bindings.
Topology makes it take a second input and emit one wiring **per group**.

The six placement rules are all statically checkable against the feature-file, so
they are generation errors, not runtime races. Make the bad state
unrepresentable, not detectable. Rules 1-5 are enforced by `validate_topology`
today; rule 6 is specified here and not yet implemented:

1. **Projections for one model share a group, and that group runs serially.** A model
   is a fold in log order; scatter its projections and the fold interleaves — silently,
   and only under load. This is the rule a plausible-looking config is most likely to
   break. N workers in one group, or one worker holding N units in flight, interleave
   that same fold exactly as two groups do, so the rule is about concurrency and not
   merely about group count: a group holding any projection is pinned to one lane.
2. **Policies partition freely when they read nothing.** They react and dispatch;
   commands dedupe on deterministic ids, so observing events in another group's order
   is harmless. That argument is about event order and nothing else, so it stops where
   the policy starts reading: a policy declaring `queries:` decides on a model exactly
   as a procedure does, and rule 3 gates it identically.
3. **An element partitions when it reads nothing.** A procedure or policy declaring no
   `queries:` computes its output from the command or event alone — no concurrent peer
   can reach it, so it parallelises freely. One that *does* query is a read-modify-write
   over a model a peer may be changing underneath it; with no aggregate key in the
   feature-file there is nothing to serialise it by, so a group holding one runs
   serially. The discriminator is the `queries:` list, never the kind of the element
   that declares it. Per-aggregate keys are what would relax that second case, and until
   they exist a reader is the only thing that pins a group to one lane.
4. **Queries are not in groups.** They are reads over a model. Placement is a
   question about the read substrate, not the log — which is why `ElementKind`
   has no `queries` value. A selector reaching a query name is refused, with the one
   exception the name-space forces: nothing makes names unique across kinds, so an
   exact `names:` entry that is *also* a declared procedure, policy or projection
   unambiguously claims that element and is accepted. A glob gets no such exemption —
   a pattern that sweeps up a query name reads as claiming the query, and is refused
   even when it matches elements too — and neither does an exact name that resolves to
   no element at all.
5. **Every element is claimed by exactly one group.** Two claims and the element
   belongs to nobody — neither group's worker can assume it ran, and the rule-1
   fold order stops meaning anything. No claim and it never runs at all, which
   is the failure a `catch_all` group exists to make impossible; at most one
   group may be that group. This is the rule the shipped validator's messages
   name, and it is checked before any of the others, because a doubly claimed
   element would otherwise cascade a rule-1 error on top of the message naming
   its real problem.
6. **A reader shares a group with what it reads.** An element declaring `queries:`
   decides *on* a model, and what the log records is the decision, never the model. A
   reader whose group trails the group folding a model it reads does not merely see
   stale data — it emits a *different event*, so two deployments' logs diverge in
   content rather than converging late, and no refold repairs a log that is short an
   entry. So the group claiming an element must also claim every projection folding
   every model that element's queries read. Discriminated exactly as rule 3
   discriminates, and the two are the same fact on two axes: rule 3 says a reader
   forbids concurrency *within* its group, this one says it forbids *distance* from
   the model it reads. An element that reads nothing is untouched by both, which is
   why the paid `*_completion` tier and the 32-wide `expand_*` tier survive unchanged.

Co-membership is the only cure for that divergence that stays static, and it works
because inside one group there is one cursor and one lane — rules 1 and 3 already pin
any group holding a projection or a reader to a single lane — and a group worker folds
and commits before running the elements it claims at that position, which is the
guarantee `Engine._drain_events` enforces today with four sequential statements and a
group worker must inherit.

The closure rule 6 induces does not chain, and that is structural rather than lucky: a
projection declares no `queries:`, so no reader is reachable from another reader through
a model. The constraint graph is bipartite — readers on one side, model cohorts on the
other — and two cohorts merge only where one element's own `queries:` list spans both.
The rule is also deliberately silent about *writes*. An element's emissions go to the
log, which is the truth and is ordered; a projection folding them late is latency, not
divergence. Pinning emitters to the folds of what they emit is what would make the
closure transitive: on `examples/recipes` it unions all four models and drags every
`record_*` procedure in via its own catalog fold, collapsing all 20 elements into one
group. The silence about writes is load-bearing, not an omission. Queries stay exempt
(rule 4) for the same reason the rule stops at reads: a stale read served to a person is
latency, a stale read served to a *decision* is divergence, and only the second is the
log's problem.

What rule 6 costs is not throughput — the set it constrains is exactly the set rule 3
had already pinned to one lane — but the freedom to put a decision anywhere other than
with the folds it reads, and on a read-heavy feature that is a real cost paid by the
groups least able to afford it. On `examples/recipes` the readers are `open_batch`
{catalog, inventory}, `run_batch` {batches, catalog, inventory} and
`advance_ready_batches` {batches}, which fuses 10 projections and all 3 readers into one
13-element group. That leaves seven elements in six further placements — the five
`record_*` procedures are free, but `generation_graph` and `derivation_graph` both fold
`provenance` and so are one unit by rule 1 — for a maximum of seven groups, not one.
Thin, and a property of recipes (a chained workflow where nearly every decision reads
shared state) rather than of the rule.

**Both worked topologies above pay for it, and the isolation one is illegal as
written.** `fast` claims *every* projection, so every querying procedure the `default`
catch-all sweeps up — `open_batch` and `run_batch` in the recipes shape — now breaks
rule 6, and the only legal repair is to move them onto `fast`: the group whose stated
job is to hold the lock and stay quick. What survives is the thing that topology was
bought for, because the `paid` tier reads nothing and still comes off the lock; what
rule 6 takes away is splitting *read-decisions* off it. The fan-out topology pays the
same way and not, as an earlier draft of this section said, by narrowing one selector:
`{model: artifact_ledger}` now determines where every reader of `artifact_ledger` lives,
wherever the catch-all would otherwise have put it, so the ledger group and its readers
must be co-declared. That a `{model: X}` selector reaches beyond X's projections is the
part of rule 6 most likely to surprise an author.

Failure names the element and the rule it broke. Three smaller conditions are
generation errors for the same reason the six rules are — a deployment that cannot
be what it says it is should not be representable:

- A group that can claim nothing — no `include` selectors and no `catch_all` — is a
  typo with a runtime cost: a process consuming a cursor and running nothing.
- A group that claims no element may not ask for concurrency. There is nothing in it
  to spread.
- An `include` selector must claim at least one element. An empty `names: []` claims
  none, and is reported as such rather than widening to whatever its other fields
  allow.

## What a group worker does

The loop, once per doorbell or poll:

```
read cursor  →  fetch log entries after it  →  for each, run the elements
this group claims  →  commit their effects  →  advance cursor
```

Two properties make that safe to distribute:

**The transport is a doorbell only.** It carries "wake up, there is work" and
nothing else. Correctness comes from the cursor, so a dropped notification costs
latency and never costs work. That is what makes `inprocess`, `zmq-ipc`, `nats`
and `postgres-notify` genuinely interchangeable — and what keeps them a config
value rather than an architecture.

**Cursors are durable but disposable.** Persisted so a boot is `snapshot + tail`
rather than `replay(all)`, because a log only grows. But losing one must be
*safe*, not merely recoverable: commands carry deterministic ids and procedures
guard, so replaying from zero re-dispatches everything and every one no-ops. That
property is what keeps a cursor a performance checkpoint instead of a second
source of truth, and it needs a test, not a paragraph.

What a cursor may *not* be is a position in canonical order. `DagStore.iterate` is
Kahn over the whole event set tiebroken by content-hash id, so the order is a pure
function of that set and a replicated event carrying old parents splices into the
*middle* of it: measured, one peer fact forked off an early entry renumbered 196 of
200 existing positions. `Envelope.seq` is the per-call enumeration index, nothing
stores it, and `add_replicated` never even sets it. A cursor is therefore either a
**frontier** — a set of event ids whose causal past has been folded, resumed by the
walk-back-until-known idiom `dagstore/sync.py` and `replicate.pull` already
implement — or a **local insertion watermark**, which is prefix-stable, always a
valid causal order, and the only one of the two that seeks (0.5ms against 200k
events, versus 0.5s to rebuild canonical order before it can yield anything). The
watermark is node-local and must never be compared between peers; the frontier is
the portable one. Two store defects block a tailing cursor and are named here so
they are not discovered later: `DagStore` is the one durable component in the engine
that does not set `journal_mode=WAL`, and `iterate`'s two Kahn setup statements are
separately autocommitted, so a foreign commit landing between them misclassifies a
child as a root and emits it before its parent — reproduced unforced in ~0.85% of
concurrent passes. Nothing tails the log today, which is the only reason that is
latent.

## The open question: what is a command's durable home?

This is the crux, and the draft does not settle it.

A group's cursor runs over the **event** log. But procedures consume **commands**,
and a command is not an event — it is a request that may still be refused. So
what does a procedure group's cursor actually read?

Durability cannot come from a second store. An event commits, the process dies,
the intent was never enqueued, and nothing will ever produce it — that crash
window is exactly why "add a durable queue" is the wrong answer.

The resolution that seems to follow: **record command requests in the log too.**
One substrate, one transaction. A procedure group's work is then "requests not
yet followed by their outcome", which is a projection of the log like any other,
and the cursor model needs no special case.

What it buys the fan-out consumer is more than durability. Every unit of work
becomes a fact, so "which of the thousand failed, and why" is a query over the
log rather than a grep over worker output. Dead-lettering stops being a transport
feature and becomes a recorded outcome.

What it costs is volume — roughly two log entries per unit of work instead of
one. For 1000 artifacts that is 2000 entries where there were 1000. Probably
acceptable; needs measuring before it is a decision rather than a hunch.

Three things fall out that are not yet designed:

- **Externally-originated commands.** A command from an API call has no
  triggering event. It becomes the first entry rather than a derived one — fine,
  but it means the log's genesis is not always an event.
- **Backfill for a new policy.** A policy added today cannot sensibly default its
  cursor to zero (it re-dispatches all history) or to head (it silently skips
  work). This wants to be a declared property of the policy in the *feature-file*
  — `backfill: none | from-genesis | from-heads` — decided when the policy is
  authored, not when it is deployed.
- **Aggregate keys.** Rules 3 and 6 both split elements on `queries:` — a sound
  discriminator, but a blunt one. A procedure reading a model it never writes is
  safe to parallelise and is refused anyway; two reading disjoint aggregates likewise.
  An aggregate key in the feature-file is what would let the validator say yes to
  those, and it is the difference between "reads nothing" and "reads nothing *this
  worker is racing*". It loosens both rules identically — two readers over disjoint keys
  need not share a group with each other, and a reader may sit apart from a fold it
  provably never races — so rule 6 should be written as a predicate an aggregate key
  later *narrows*, not one it replaces.

## Does this subsume `st` and `mp`?

Possibly, and that is worth deciding deliberately rather than discovering.

`st` falls out as a topology: one group, `inprocess` transport, `memory` cursors,
one worker. If that is true, `st` stops being a shell and becomes a default.

`mp` is a harder case, because its model differs rather than specialises. Its
unit of distribution is the *command*, routed at dispatch by the app's `routes()`
into broker pools, and its correctness rests on the broker's at-least-once
delivery. This design routes by *cursor over a log* and treats delivery as
advisory. Both can be true at once, but maintaining two distributed shells with
different correctness stories has a real cost — the conformance suite has to
assert both, forever.

The guarantee is the thing to compare. `test_conformance.py` currently asserts
that `st` is sequentially consistent and that `mp` deliberately is not, with a
test whose whole job is to stop `mp` being quietly promoted. The topology claim —
*same log, same feature-file, any valid topology, byte-identical models* — is
stronger than what `mp` promises. If it holds, this is not `mp` with a config
file; it is `st`'s observable guarantee at `mp`'s scale, bought by constraining
placement instead of trusting a broker.

Rule 6 strengthens that claim without establishing it. It removes the failure mode
that makes two logs differ in *content* — a decision reached against a stale model —
which is the half no refold can repair. The residual gap is not about reads at all:
`DagStore.append` names *the current local heads* as parents, so two groups appending
concurrently mint different parent tuples, hence different content-hash ids, hence a
different DAG. Since `iterate` is a deterministic function of the event set, the
claim's second half — given the same DAG, every topology folds byte-identical models
— is already true and is the convergence invariant. The unfinished half is the first:
parents must become a function of the entry the element consumed rather than of
whatever the frontier happened to be at append time. That is a change to `append`,
and it is the companion work to rule 6 rather than something rule 6 assumes away.

That claim is a property test, not an example: random valid topologies, random
interleavings, replay, compare. It is the first thing to write, because if it
does not hold the rest does not matter.
