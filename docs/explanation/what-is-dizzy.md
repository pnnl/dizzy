# What DIZZY is (and why events)

You have written this code:

```sql
UPDATE orders SET status = 'refunded' WHERE id = 41;
```

It is correct, it is fast, and it destroys evidence. The row now says `refunded`. It does not say when, or who asked, or what it was before, because the update overwrote all of that.

So you add an `audit_log` table and write a row to it next to the update. Now there are two writes and only one of them is enforced by the schema, so the day someone adds a second code path that sets `status`, the log quietly stops being complete. Then product asks for an email when an order is refunded, and that hook gets bolted onto whichever update site you happened to be looking at. Then finance asks how many refunds happened per week last year — a question the `status` column was never able to answer, and the log answers only in whatever shape you happened to write it.

None of these are bugs. They are one design decision coming back four times: you made the **current value** the truth, and treated **what happened** as a side effect you maintain by hand.

DIZZY is built on the opposite decision. It costs more moving parts than a `status` column, and the rest of this page is the honest trade. You need SQL and a web framework to follow it; there is nothing to install and no code to run.

## Turning it around

Stop storing the current value. Store what happened, as an append-only list of facts: *order 41 was refunded, at this time, on this request*. Nothing in that list is ever edited or deleted, because a fact that already happened cannot stop having happened. The `orders` table does not disappear — it gets demoted. You derive it by walking the list from the beginning and applying each fact in turn, which makes it a cache: delete it, rebuild it, and you have lost nothing. History is no longer an optional extra you remember to maintain; it is the only thing there is, and the table is the thing you can afford to lose.

That re-answers all four of the cracks above. When and who: the fact carries them, because they are part of what happened. Drift between the table and the log: impossible, since there is one write and the table is computed from it. The bolted-on email: it reacts to the fact, not to whichever call site you found. Last year's weekly refund counts: replay the facts into a new table shaped for that question, over history you already have.

Storing facts and deriving state from them is called being **event-sourced**. That is the whole idea; everything below is vocabulary for its parts.

## Naming the pieces you already have

The system just described has seven kinds of moving part, and DIZZY gives each one a name.

- **command** — a request that has not happened yet; it can be refused. `refund order 41`
- **procedure** — the one function allowed to decide whether it happens. It validates the request, does the work, and records the outcome.
- **event** — the record that it did. Past tense on purpose; never edited, never deleted. `order_refunded`
- **projection** — folds events into a table: it *applies each fact in turn to a running table*, which is what "fold" means here.
- **model** — that table. Derived, disposable, rebuildable from the events at any time.
- **query** — reads the table back out. `list_refunds`
- **policy** — reacts to a fact by issuing another command. It never records a fact itself. "When refunded, email the customer."

The split between the first and the third bullet is the one that does the work:

**Commands are requests and can fail; events are facts and cannot.**

Everything else follows from that. A procedure is where a request becomes a fact, so it is the only place a decision lives. A policy issues a command rather than an event, so a reaction is still subject to refusal — an email that cannot be sent does not falsify the refund. And a projection consumes facts only, so a table can never contain something that did not happen.

## Two loops

Follow the refund through, name by name:

```
refund_order ─▶ process_refund ─▶ order_refunded ─▶ notify_customer ─▶ send_email
order_refunded ─▶ refund_ledger ─▶ orders ─▶ list_refunds
```

The request `refund_order` reaches `process_refund`, which decides and records `order_refunded`; the policy `notify_customer` sees that fact and issues `send_email`, a new request that starts the loop again. On the second line, the same fact reaches the projection `refund_ledger`, which folds it into the `orders` model, which `list_refunds` reads.

In general terms, that is:

```
Commands  ─▶  Procedures  ─▶  Events  ─▶  Policies  ─▶  Commands   (reactivity loop)
Events    ─▶  Projections ─▶  Models  ─▶  Queries   ─▶  Procedures (data loop)
```

The data loop's tail runs back into Procedures because a decision usually has to consult what is already known: `process_refund` may need to read the order before it can refuse.

## A whole feature, in one file

Those seven kinds of thing, and which of them connects to which, *are* the whole design of a feature. Nothing else about it is design — the rest is implementation. So DIZZY puts exactly that in one file and generates from it. Here is the feature you build in the guestbook tutorial:

```yaml title="guestbook.feat.yaml"
--8<-- "tutorials/guestbook/guestbook.feat.yaml"
```

Four keys in that file are the arrows from the diagram above, written down. On the procedure, `command:` names the one request `record_signature` accepts and `emits:` names the one fact it is allowed to record. On the projection, `event:` names the fact it folds and `model:` names the table it folds into. On the query, `model:` names the table it reads. Every other key is a description or a name. (`adapters: [sqla]` is the storage adapter — how this model is reached; ignore it for now, the tutorial covers it.)

The guestbook declares no `policies:`, because nothing in it needs to happen *because* something else happened; signing is the end of the story. For a feature where that is the point, see [`examples/recipes/`](https://github.com/PNNL/dizzy/tree/main/examples/recipes): its `advance_ready_batches` policy reacts to `entity_produced`, consults a query for what is waiting, and dispatches `advance_batch`, so one produced item starts the next batch and the cascade runs itself. That example also runs on the generated wiring stage, which no tutorial introduces yet.

## What that file turns into

Those four keys are declarations. Here is what they generate. The procedure's context, from `lib/python-uv/gen_int/`:

```python
@dataclass
class record_signature_emitters:
    guestbook_signed: Callable[[GuestbookSigned], None]


@dataclass
class record_signature_context:
    emit: record_signature_emitters
```

And the stub you fill in, from `lib/python-uv/procedure/record_signature/`:

```python
def record_signature(
    context: record_signature_context,
    command: SignGuestbook,
) -> None:
    raise NotImplementedError
```

`record_signature` receives exactly one kind of command and can emit exactly one kind of event, because the feature-file said so. That is the whole thesis, shown rather than argued.

The feature-file supplies the names and the connections; the fields on each command and event are authored separately, in the LinkML schemas DIZZY scaffolds under `def/`. That second pass is stage two of the tutorial.

DIZZY also generates the code that routes commands to procedures and events to projections, so you never hand-write that wiring.

## When this is worth it

What you get: state you can rebuild from scratch after any bug in a projection; a new table answering a new question, computed over history you already have; an answer to "why is this in this state" that is a list of facts rather than a guess; and an audit trail you cannot forget to write, because writing it is the only write there is.

What it costs: more moving parts than a table with a `status` column, a second authoring pass in `def/` before anything compiles, and a vocabulary your team has to learn before they can read the file. It pays when history, auditability, or reacting-to-change is the point of the system. It does not pay for a CRUD form over a handful of rows.

DIZZY is research code. The `python-uv` path is the most complete; `rust-cargo` and `typescript-npm` are experimental.

The long-form argument is in the [whitepaper](whitepaper.md).

## Next

You have just read the design of the feature you are about to build. **[Build a guestbook](../tutorials/guestbook.md)** takes that exact file from an empty directory to a running demo — every command and every line of output on that page is executed and checked, so it cannot drift from the tool.

The [feature-file format](../reference/SPECIFICATION.md) documents every section and field.

`dizzy docs authoring` prints the agent-facing guide to writing one.
