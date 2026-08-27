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
is not just labthing's problem in YAML. Both are shown here as written against the
schema as it stands, and rule 6 below costs each of them an edit — said there rather than quietly fixed
here, because what the edit costs is the argument:

```yaml title="isolation"
groups:
  - name: fast          # holds the lock; must stay quick
    include: [{kind: projections}, {kind: policies}]
  - name: paid          # 40s turns block nothing else
    include: [{kind: procedures, names: ["*_completion", "summarize_*"]}]
    executor: pool      # a background lane, off the lock entirely
    workers: 4
    retry: {max_attempts: 3, backoff: exponential, dead_letter: true}
  - name: default
    catch_all: true
priority:               # command TYPES, not chains
  interactive: [send_notification, "approve_*"]
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

## Two latencies, two knobs

`executor` and `priority` are the whole of the single-machine placement vocabulary,
and they exist because "the command was slow to start" is two unrelated problems that
each need their own fix:

- **Queue position.** An urgent command sits behind two hundred batch commands.
  `priority` fixes this: an interactive command is claimed before any batch command
  waiting on the same lane.
- **Head of line.** A forty-second procedure is *already running*, so the queue is not
  consulted at all until it finishes. Priority cannot help — there is nothing to
  reorder. Only `executor: pool` fixes this, by moving that procedure to a background
  lane served by a spawned process pool, so the main lane keeps claiming.

Neither knob solves the other's problem, which is why there are two of them rather
than one number.

**Priority has exactly two levels, `interactive` and `batch`, and batch is the
default.** Not an integer: a numeric priority has no semantics anybody can state
without running the scheduler, and every such field ends in teams bidding their own
work up. Two levels have a one-sentence definition, and that sentence is testable.

**Priority keys on command names, and does not travel along the causal chain.** It is
a property of the command *type*. A policy reacting to an interactive command's event
dispatches whatever the command type it names is declared to be — inheritance would
make latency depend on the route a command arrived by, which is precisely the thing a
topology exists to state rather than to discover under load. Note the name-space: the
`include` selectors above match *procedure* names, `priority` matches the `command:`
values those procedures consume, and in a feature-file where procedure `open_batch`
handles command `start_batch` the difference is visible. Naming the wrong one is a
generation error rather than a silent no-op.

**Pool eligibility is not a new question.** A background lane runs while the main lane
runs, so `executor: pool` is a concurrency claim exactly as `workers: 4` is, and it
goes through the same gate: rule 1 refuses a pool group holding a projection, rule 3
refuses one holding any element that declares `queries:`. What an element *reads* is
what decides whether it may leave the lock, and there is deliberately no second answer.
That cuts both ways on the recipes shape, where the two slowest procedures
(`open_batch`, `run_batch`) are exactly the two that query — "slow" and "forkable" are
not the same predicate, and the schema will not let you pretend they are.

The default topology — no `topology.yaml` at all — is one inline group with every
command batch, which is byte-identically the behaviour of a build from before any of
this existed. `dizzy generate wiring` emits no placement table for it.

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
guarantee `Engine.ingest` enforces today with four sequential statements and a
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

A group is a unit of *serialization*, not a unit of intake. It has one lane of
execution and up to two intakes.

**Event intake — a cursor.** A group claiming projections or policies holds one
cursor over the log:

```
read cursor  →  fetch log entries after it  →  fold this group's projections  →
commit  →  run this group's policies  →  advance cursor
```

That is `Engine.ingest`'s body with the append taken out of it, in the
same order, so the four sequential statements survive the split verbatim.

**Command intake — a lane.** A group claiming procedures holds one lane of the
durable command queue, named after the group. No cursor is involved: a command
is not in the log, so there is no position to hold. A policy's dispatch is
routed to the lane of the group claiming that command's procedure, from a table the
generator computes out of the topology — so nothing in `engine/` learns a command
name. That table is `COMMAND_PLACEMENT` in the generated wiring: a mapping from
`type(command).__name__` to `(lane, priority)`, beside a `LANES` mapping giving each
lane its executor and worker count. It is generated **data**, never a hand-written
callable, because a callable is a second place the deployment is described and it
drifts from `topology.yaml` in silence.

What consumes it is `dizzy.engine.st.executor.LaneExecutor`, which reads `LANES`
verbatim: one inline lane served in the calling thread, one background thread and one
spawned process pool per `pool` lane, all over the single durable queue, with the log
still written by exactly one thread. What does not exist yet is the last hop — no
generated wiring constructs that executor, so a host that wants its lanes served today
builds one itself from the table the wiring already emits.

**Most groups are both**, because rule 6 fuses readers with the folds they read
and readers are mostly procedures. A hybrid worker's cycle is **fold to tail →
run one command → fold to tail**. The leading fold is what carries the central
guarantee across a group split: a policy-dispatched command is causally after
the append of the event that triggered it, and positions are local insertion
order, so a worker whose fold covers the command's arrival in the lane has
necessarily already folded that command's triggering event. It needs no
coordination, but it does need the fold to be taken *after* the claim, and
`GroupWorker`'s command intake is today one opaque callable that claims and
runs together — so a claim that blocks returns after the fold, and an event
appended during that wait is not in the model the command reads. Closing that
window means splitting the intake into claim and run; until it is, the property
this paragraph states is the intent and not yet the behaviour. The trailing
fold is for `run_command`'s own post-condition —
that projections have folded by the time it returns — which every host that runs
a command and then renders depends on.

The guarantee that survives the split is not quite the one `ingest`
states. It is: **the read model commits before the dispatched command runs.**
Inside one group those are the same sentence; across groups only the second is
available, and it is the half anything downstream actually needed. What is given
up with it: a hybrid group folds to *tail*, so it also folds entries another
group appended that a sequential run would have ordered later, and a procedure
branching on that reads a model further ahead than `st` would have handed it.
Models still converge — that is confluence — but two topologies' logs need not
agree entry for entry, which is the residual the last section names.

The expectation that goes with rule 6 — and it is an expectation, with no
harness in the tree yet to hold it: over randomized interleavings of a fixture,
one hybrid group should reproduce `st`'s read models *and* its stream entry for
entry, a rule-6-legal split should diverge in neither, and a layout that
violates rule 6 should diverge in some of them by reading an empty model,
dispatching nothing, and killing a cascade permanently. That is what the
property test named at the end of this document is for; no number here has been
measured, and none should be quoted until it has.

The cost is a fairness policy the earlier draft never contemplated. A hybrid
worker chooses between draining its lane and advancing its cursor;
commands-first starves folding under replication load, folds-first starves
commands under fan-out. Whatever that rule is, it is part of the defined
semantics the way the shell already is.

Two properties make the loop safe to distribute:

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


### What `workers: N` buys, per kind of group

`save` is monotone, which makes a cursor one lane's watermark and *not* a thing
two workers may split: the leader's save covers a prefix the laggard never
committed, and the crash loses that work silently. So the knob answers three
different ways, and conflating them is how it becomes a lie.

- **A group holding a projection or any reader.** Refused, as today — rules 1
  and 3 pin it to one lane and `_effective_concurrency` already errors.
- **A procedure-only group.** N workers on one lane, and genuinely just
  operations — with one prerequisite. `claim` is a `SELECT` then an `UPDATE`
  guarded by a `threading.Condition`, which serializes one *process*, not one
  machine: two connections on one file claim the same row. Until it is a
  conditional `UPDATE ... WHERE status='queued'` with a rowcount retry,
  `workers: 2` runs concurrently what the operator was told was merely retried.
- **An event-only group of non-reading policies.** N cursors, not one. Worker
  *k* of *N* owns `<group>#k/N`, reads the whole stream from its own position,
  acts on the entries whose content hash mod N is *k*, and saves on every entry
  whether it acted or skipped — a skipped entry is done, by ownership. Each
  watermark is then honest and a crash re-delivers one shard's tail. The price
  is that changing N invalidates every assignment: the only safe reshard is to
  rewind all N cursors to the lowest and replay, which is safe only because
  those policies read nothing and commands dedupe on deterministic ids — the
  property this document now rests on twice and still has no test for.

Rejected: one cursor, N workers, disjoint batches. That is the at-most-once hole
`save`'s monotonicity hides rather than fixes.

## Where a command lives — decided, and what was deferred

Two answers were built and measured against each other. This is the one that
lands; the reasoning matters more than the verdict, because the other one may
still be right later.

**Decided: a command's durable home stays the queue, and a procedure group has
no cursor.** A group gets a cursor if and only if it claims something that
consumes the log. Procedures consume commands, commands stay in
`st.DurableCommandQueue`, one lane per group. The question "what does a
procedure group's cursor read?" is answered by refusing its premise rather than
by inventing a reading.

That narrows the crash window without a second substrate. A cursor-driven policy
group saves its cursor only after the `put` has committed, so a crash between an
event's append and the enqueue leaves the cursor unadvanced, the policy re-runs
on the same entry, and the command is re-dispatched. For the reactivity loop the
window that made "add a durable queue" the wrong answer is shut.

What stays open, said plainly:

- **Externally-originated commands.** An API call that dies before its `put`
  commits is gone, with no log entry to re-derive it; only a client retry
  recovers it. The log's genesis still is not always an event.
- **A new hazard, from the cursor rather than despite it.** `command_queue.db`
  is a second file beside the event store. Lose it and every queued, unclaimed
  command vanishes while the policy group's cursor has already advanced past the
  events that produced them — a durable record asserting work that never
  happened. Today `st` has the same file and no cursor to be wrong about, so a
  rebuild recovers. Under this design, dropping that file must be paired with
  rewinding every policy group's cursor. An operational coupling between two
  stores is exactly the smell this section opened by naming: it has been moved
  and made quieter, not removed.

**Deferred: recording command requests in the log.** It is still the better
answer, and it was built far enough to say precisely why it is not this
increment's. Shaped as a parentless entry in a reserved type namespace —
edgeless, so it never enters ancestry, never replicates, and never perturbs
canonical order — it passes the conformance suite unmodified, and "requests not
yet followed by their outcome" becomes an ordinary read model, rebuilt from zero
in tens of milliseconds. Three things have to be true first:

- **The back-reference is inside the hash.** Naming the request in the event's
  wrapper changes every caused event's id, because the wrapper *is* the hashed
  payload. That is a hash-format decision, not a storage one, and it belongs in
  its own change.
- **`heads()` has to learn a type predicate**, or parentless requests become
  permanent heads and the next append drags them back into ancestry. Editing the
  correctness kernel is not a side effect of a topology increment. `iterate`,
  `__len__` and `seq` need the same filter, and every omission is silent in a
  different way.
- **`append` is quadratic** — `heads()` is an unindexed anti-join run inside
  every append, so doubling the entries per unit of work more than doubles the
  ingest cost. A maintained frontier table flattens it, is worth doing on its
  own, and is a prerequisite here.

One variant must not be revived: parenting each entry on the entry its element
consumed. It turns the chain into a branching DAG, canonical order then
tiebreaks concurrent siblings on a hash containing `ingested_at`, and two runs
of the same script iterate in different orders — which fails
`test_the_sequential_shell_is_deterministic` and, worse, breaks the convergence
invariant the DAG exists to provide. Dropping `ingested_at` from the hash
repairs it, at the cost of the append time no longer being part of the
hash-protected fact. That is a decision on its own evidence, not a step in this
one.

Two things named earlier are still not designed:

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
one worker. It is a *hybrid* group — it claims procedures, so it holds a lane as
well as a cursor, and both intakes are served by the one thread. That is the
degenerate case of the loop above and not a special one, which is the whole
reason to believe it: `st` stops being a shell and becomes a default.

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
