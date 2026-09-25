# SerenHippocampus

The sleep cycle for [SerenMemory](https://github.com/ChadRoesler/SerenMemory),
split out into its own service. It holds no store. It reads short-term
memories from Memory, writes **dockets** to Memory, resubmits what the
reviewer denied, and purges what was flagged.

Port **7424**, the last slot in the brain band (Memory 7420, Margin 7421,
Loci 7422, Corpus Callosum 7423).

## The shape

A draft is not one synthesis that becomes one long-term entry. A draft is a
**docket**: a list of operations on long-term, each reviewed on its own by
the main model.

| operation    | what it means                                                                 |
|--------------|-------------------------------------------------------------------------------|
| `new_core`   | a durable statement with the lesson in it                                     |
| `attach`     | tonight's short-terms are more evidence for a core that exists; its evidence grows, its wording may be restated |
| `supersede`  | yellow arrives; blue becomes a demoted satellite of yellow's story, still recallable through history |
| `verbatim`   | "this deserves its own thing and must not be muddled" - one episode, kept word for word |

Long-term is **a core and its surroundings**. Recall returns cores.
Satellites are the supporting episodes with their dates; a superseded core
keeps `superseded_by` pointing forward.

The small model (the Inside Out workers) drafts the docket. The main model
reviews it per operation through Memory (`POST /dockets/{id}/review`, or the
`review_docket` MCP tool), approving or denying with a critique. Memory
applies what is approved. Denied operations come back here: the **tend**
loop redrafts them from the critique and resubmits as the next attempt. The
last permitted attempt is submitted `terminal`, which is the reviewer's cue
that edit-on-approve is now allowed - the release valve, never the loop's
shortcut.

Forget is separate. A flag on a long-term memory means *purge*: executed at
the next sleep with a tombstone (id, reason, time, what the cascade removed -
never the content), or immediately through Memory's `purge_memory_now` for
the emergency.

## The loop

Every few minutes (`tend_interval_seconds`) one tick runs, two steps:

- **tend**: reviewed dockets with denied operations and no later attempt
  in their chain get redrafted from the critique and resubmitted; a chain
  whose every operation has a verdict (or whose last attempt was denied) is
  **culled** - the dockets closed, the brief that opened it consumed, the
  review note completed, a `chain_closed` event.
- **check**: purge what was flagged; then ask Memory for an open brief.
  One there and no chain open means **sleep now**. None means wait.

**The brief is the gate.** The hippocampus never drafts on its own clock. A
sleep: group the free short-terms by topic, show the small model each
cluster with the nearest existing cores and the brief as the steer, submit
one docket, leave a review note in Memory naming it, then the mechanical
tidy (age out, maintain near-term, sweep pruned). Short-terms a pending
docket already holds are left alone. One chain at a time: a brief that
arrives while a docket is under review waits its turn.

`sleep.mode: thread` runs the tick here; `external` means something else
POSTs `/tend` and `/check` (or `/sleep` by hand).

### When the sleep fires

Whenever a brief arrives. Writing one at noon and saying "I'm off to the
forest" puts the hippocampus to bed at noon.

**Bedtime** is `interval_seconds` after the last sleep, or the next
`sleep.at` wall-clock time (local `HH:MM`). Past bedtime with no brief, the
checks are counted as misses, and every `brief_check_misses` of them the
hippocampus asks: "I see you're looking tired, let's get ready for bed." It
has no line to a model session, so it leaves an intent in Memory's
near-term tier - the one place the main model is guaranteed to look at the
start of a session - and emits a `brief_requested` event for whatever
listens on the webhook. Asked and still waiting, it nudges the webhook
again but leaves no second note. `/status` carries `bedtime_at`,
`brief_wanted` and the last check.

### The brief

The main model writes it (`submit_brief` on Memory: a summary of what
mattered, `promote_hints`, `noise_hints`, `completed_intents`). It is how
the hippocampus is not making every decision alone, and it is what turns a
one-off joke into an inside joke. A matched promote hint drops a topic's
promotion threshold to one and a matched noise hint raises it out of reach,
so a running bit becomes a core from few fragments and a one-off never does.
The summary itself, plus the hints that matched this cluster, go into the
drafting worker's prompt as the *steer*, marked as written by the main model
or pulled from the fragments. A brief steers the one sleep it opened and is
consumed when that chain lands (or at once, if there was nothing to draft).
A one-off mention of a fish dream stays short-term. A consistent bit about
Fred Durst as the warrior poet of our time becomes a core, because the brief
named it.

`brief_pull: true` is for a headless install with no main model: after
`brief_check_misses` misses the small model pulls a brief from the fragments
and sleeps on it. Off by default, by design.

### The month away

A month with no brief is a month with no sleeps: nothing drafted, nothing
aged out. Flagged memories are still purged on every tick, because a flag
is a decision already made. The first sleep back (a gap longer than
`gap_grace_intervals` bedtimes, or the first sleep ever on a store) is a
**catch-up**: it drafts and purges but does not age out and does not sweep,
so nothing that never had its chance is gone before someone has looked.

### Quiet

A sleep whose brief finds nothing free to draft from calls no model,
consumes the brief, tidies, and records itself as `quiet`.

### The model, on only when needed

The drafting model runs a few minutes a night and after a review; it should
not hold VRAM the rest of the day, on a Nano floor or on a 4 GB card. With
`model.lifecycle.manage: true` the hippocampus starts the server when a sleep
or a redraft needs it (a `start` command line, or the node's Observatory for a
registered service), waits for its health check, keeps it warm for
`keep_warm_seconds` after the last call, and stops it on the next tick after
that. It never stops a server it did not start: one that already answered was
started by someone else.

A configured model that cannot be reached is a failed sleep that keeps its
brief for the next check - never a mechanical copy of the fragments. After a
failed start the next attempt waits `retry_after_seconds`. `/status` carries
`model_lifecycle`; events `model_started`, `model_stopped`,
`model_start_failed`.

## Mechanical mode

With no model configured (`model.url: ""`) the hippocampus still sleeps:
clusters over `promote_min_evidence` become `new_core` operations by their
longest entry, verbatim flags are honoured, flagged memories are purged.
It never proposes `attach` or `supersede`, because deciding that tonight's
fragments are more evidence for core 41, or that yellow replaces blue, is a
judgement, and a threshold is not one.

## Install and run

```bash
pip install seren-hippocampus            # + [mcp] later, + [corp] behind a TLS-intercepting proxy
cp seren-hippocampus.yaml.sample ~/seren-hippocampus/seren-hippocampus.yaml
python -m seren_hippocampus --config ~/seren-hippocampus/seren-hippocampus.yaml
```

The config has five blocks: `server` (this service's bind and bearer),
`memory` (where SerenMemory is and the bearer to present to it - the same
three pointers as every server block), `model` (the small model's
OpenAI-compatible endpoint), `sleep` (intervals or a wall-clock time,
thresholds, attempts, the catch-up grace), `notify` (below).
Beyond loopback with no bearer the service refuses to start, like every
Seren service.

## API

| route          | what                                                  |
|----------------|-------------------------------------------------------|
| `GET /`        | service, version, mode, whether a model is configured |
| `GET /health`  | liveness, and whether Memory answers                  |
| `GET /status`  | last sleep and last tend, intervals                   |
| `POST /sleep`  | sleep now by hand, on the open brief if any (409 if busy) |
| `POST /tend`   | pick up denied operations now; cull chains that landed |
| `POST /check`  | check for a brief now; sleep on it if there is one    |
| `GET /queue`   | what is waiting for review, read from Memory          |
| `GET /history` | the last runs, newest first                           |
| `GET /events`  | what happened, newest first, and whether it was sent  |
| `GET /viewer`  | the window (below)                                    |

## Saying what happened

Every sleep and tend leaves **events**: `docket_submitted` (with the docket
id and its operation count), `sleep_failed`, `sleep_done`, `purged` (ids,
never content), `tend_resubmitted`, `chain_ended`, `catch_up`. They are kept
on the service (`GET /events`, the History tab) whatever else is configured.

With `notify.webhook_url` set, each event in `notify.events` is POSTed as
JSON with the configured bearer, best effort: a webhook that does not answer
is recorded on the event and never fails the sleep. This is the seed of
"shoot me a text": Lodestar, Symposium, or a messaging bridge sits at the
other end and decides who to tell. Until something does, the reviewer pulls -
`list_dockets` on Memory at the start of a session shows what is waiting.

## The window

`/viewer` is for whoever runs this and did not build it. It answers three
questions on the shared Seren shell: did the sleep run (and when is the next
one), what is waiting for review in Memory, and when it broke, what broke.
Two buttons, Sleep now and Tend now, and a plain-words explanation of the
state the service is in - Memory unreachable, mechanical mode, external mode,
the last error. The accent is wisteria (`#c9a0dc`): a climber that hangs its
blooms off a structure that was already there.

## Tests

```bash
pip install -e ".[dev]"
python -m pytest tests -q
```

The tests run the **real SerenMemory app in-process** behind the
hippocampus's HTTP client, so what they exercise is the contract between the
two services. Only the small model is faked.

## License

GPL-3.0-only. See `LICENSE`.
