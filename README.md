# SerenHippocampus

The sleep cycle for [SerenMemory](https://github.com/ChadRoesler/SerenMemory),
split out into its own service. It holds no store. It reads short-term
memories from Memory, writes **drafts** to Memory, resubmits what the
reviewer denied, and purges what was flagged.

Port **7424**, the last slot in the brain band (Memory 7420, Margin 7421,
Loci 7422, Corpus Callosum 7423).

## The shape

What a sleep proposes is not one synthesis that becomes one long-term entry.
It is a **draft**: a list of operations on long-term, each reviewed on its
own by the main model.

(Called a *docket* until 25 Sept 2026. In Probe and the Corpus Callosum a
docket is the briefing packet a search hands back, so here it is a draft.
This version talks to Memory's `/drafts` routes, which Memory has only from
the same date: **upgrade Memory first**. Memory keeps `/dockets` as an alias
for a release, so an older hippocampus keeps working against a newer
Memory, not the other way round. A `notify.events` list that still names
`docket_submitted` subscribes to `draft_submitted`.)

| operation    | what it means                                                                 |
|--------------|-------------------------------------------------------------------------------|
| `new_core`   | a durable statement with the lesson in it                                     |
| `attach`     | tonight's short-terms are more evidence for a core that exists; its evidence grows, its wording may be restated |
| `supersede`  | yellow arrives; blue becomes a demoted satellite of yellow's story, still recallable through history |
| `verbatim`   | "this deserves its own thing and must not be muddled" - one episode, kept word for word |

Long-term is **a core and its surroundings**. Recall returns cores.
Satellites are the supporting episodes with their dates; a superseded core
keeps `superseded_by` pointing forward.

The small model (the Inside Out workers) writes the draft. The main model
reviews it per operation through Memory (`POST /drafts/{id}/review`, or the
`review_draft` MCP tool), approving or denying with a critique. Memory
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

- **tend**: reviewed drafts with denied operations and no later attempt
  in their chain get redrafted from the critique and resubmitted; a chain
  whose every operation has a verdict (or whose last attempt was denied) is
  **culled** - the drafts closed, the brief that opened it consumed, the
  review note completed, a `chain_closed` event.
- **check**: purge what was flagged; then ask Memory for an open brief.
  One there and no chain open means **sleep now**. None means wait.

**The brief is the gate.** The hippocampus never drafts on its own clock. A
sleep: group the free short-terms by topic, show the small model each
cluster with the nearest existing cores and the brief as the steer, submit
one draft, leave a review note in Memory naming it, then the mechanical
tidy (age out, maintain near-term, sweep pruned). Short-terms a pending
draft already holds are left alone. One chain at a time: a brief that
arrives while a draft is under review waits its turn.

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

The easy way to set it up is to name the server and the model file, and let
the hippocampus build the command line itself:

```yaml
model:
  url: "http://127.0.0.1:7200/v1"          # host and port for the server come from here
  lifecycle:
    server: "C:\\llama\\llama-server.exe"  # on a node: ~/llama.cpp/build/bin/llama-server
    model_path: "C:\\models\\qwen3-4b-q5.gguf"
    server_args: "-ngl 99 -c 8192"          # the default; add -fa on, cache types...
```

Naming both turns management on. A missing file fails the sleep with its path
in the error. A hand-written `start` line still works and wins over the two.
Starwright: `--model-server` / `--model-path` / `--model-args`.

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
pip install seren-hippocampus            # + [mcp] for the tools below, + [corp] behind a TLS-intercepting proxy
cp seren-hippocampus.yaml.sample ~/seren-hippocampus/seren-hippocampus.yaml
python -m seren_hippocampus --config ~/seren-hippocampus/seren-hippocampus.yaml
```

The config has six blocks: `server` (this service's bind and bearer),
`memory` (where SerenMemory is and the bearer to present to it - the same
three pointers as every server block), `model` (the small model's
OpenAI-compatible endpoint), `sleep` (intervals or a wall-clock time,
thresholds, attempts, the catch-up grace), `notify` and `ripple` (below).
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
| `/mcp`         | the MCP tools (below), with the `[mcp]` extra         |

## The tools, for the main model

The reviewer is a model in a session, and a session does not reach the
viewer. It could always see the drafts and write the brief - those are
Memory's tools, because the record lives there - but not the sleep itself:
did last night's run fail, is a chain still open, when is bedtime, is the
model even up. With `pip install seren-hippocampus[mcp]` (Starwright:
`--mcp` / `-Mcp`) the same process serves an MCP endpoint at `/mcp`, behind
the same bearer as every other route:

| tool            | what                                                                    |
|-----------------|-------------------------------------------------------------------------|
| `sleep_status`  | where it stands in one call: last sleep and tend, bedtime, a brief waiting, a chain open, drafts to review, the model up or down - and a sentence saying so |
| `sleep_now`     | sleep now on the open brief. The loop sleeps by itself when a brief is open; this is for not waiting for the tick |
| `tend_now`      | redraft what was just denied, cull chains that landed, now instead of next tick |
| `check_now`     | one tick by hand: purge, then sleep only if the gate is really open      |
| `sleep_history` | the last runs, newest first                                             |
| `audit_sleeps`  | per-model numbers and the last few chains - verdicts and critiques, not the wording (that is Memory's `audit_drafts`) |
| `list_replays`  | which drafts kept their prompts                                         |
| `replay_draft`  | one draft's prompts on a candidate model, side by side; nothing reaches Memory |

A sleep or tend already running comes back as `busy`, and a Memory that
does not answer as a message - not an exception, and not a failed sleep on
the record. The tools call the same Hippocampus the routes do, off the event
loop, so a sleep started from a session holds the same lock as one the loop
started. `SEREN_HIPPOCAMPUS_MCP_MOUNT` moves the endpoint;
`SEREN_HIPPOCAMPUS_MCP_ALLOWED_HOSTS` turns the SDK's host check back on.

## Saying what happened

Every sleep and tend leaves **events**: `draft_submitted` (with the draft
id and its operation count), `sleep_failed`, `sleep_done`, `purged` (ids,
never content), `tend_resubmitted`, `chain_ended`, `catch_up`. They are kept
on the service (`GET /events`, the History tab) whatever else is configured.

With `notify.webhook_url` set, each event in `notify.events` is POSTed as
JSON with the configured bearer, best effort: a webhook that does not answer
is recorded on the event and never fails the sleep. This is the seed of
"shoot me a text": Lodestar, Symposium, or a messaging bridge sits at the
other end and decides who to tell. Until something does, the reviewer pulls -
`list_drafts` on Memory at the start of a session shows what is waiting.

## The ripple: waking the model

The webhook is the record; the ripple is the question. At bedtime the
hippocampus asks the main model for its brief, and when a draft or a redraft
lands it asks for a review - rather than waiting for the model to remember.
Named for the sharp-wave ripples a sleeping hippocampus fires to reach the
cortex. The woken model answers with its Memory tools (`submit_brief`,
`get_draft`, `review_draft`), so the cycle runs on its own:

    hippocampus: "it's bedtime - want to write a brief?"   -> the model writes one
    hippocampus: "I drafted tonight's memories - review?"  -> approve, or deny with a critique
    hippocampus: "redrafted what you denied - again?"      -> ...until it lands

It fires on `brief_requested`, `draft_submitted` and `tend_resubmitted`
(`ripple.events`), one at a time per event, and each event records what the
ripple did.

**Where the model lives decides the route.** Pick the first one that fits:

| setup | on the hippocampus |
|---|---|
| the model is on this box | `type: script` - the hippocampus runs the command itself |
| the model's box runs an Observatory | `type: endpoint`, `url: http://<box>:7777/api/v1/system/ripple`, that Observatory's bearer |
| there is a Lodestar | `type: endpoint` at Lodestar's `/api/v1/system/ripple`; Lodestar's `ripple.target` names the node (or `local`) |
| two boxes, neither installed | `type: script`, `command: ssh <box> claude -p`, `stdin: true` |

The receiving end (an Observatory with `ripple.enabled`, or Lodestar with a
`target`) runs its own configured command; a caller only ever sends the
message. With `stdin: true` the message goes on the command's stdin instead
of `{message}`, so a remote shell never parses it.

**Waking Claude Code on this box** - the script route, set up for Claude Code:

```yaml
ripple:
  type: script
  command: ["claude", "-p", "{message}",
            "--allowedTools", "mcp__wren-memory,mcp__wren-loci,mcp__wren-corpuscallosum,mcp__wren-margin,mcp__wren-hippocampus"]
  cwd: "D:\\serenDaemon\\SerenCore"     # the project the memory MCP servers are registered for
  run_as: "alice"                       # whose login `claude` uses
```

Starwright writes this for you: give the hippocampus card
`--ripple-claude <project folder>` (`-RippleClaude`). It reads
`~/.claude.json`, finds the MCP servers registered for that folder, and
writes the command with those servers' tools pre-approved and the folder as
the working directory. For it to work:

- **Claude Code is installed and logged in** as `run_as`.
- **The memory MCP servers are registered for `cwd`.** Claude Code keeps
  local-scope servers per folder; started anywhere else, the model wakes
  without its memory. (User-scope servers work from any folder.)
- **Their tools are pre-approved** (`--allowedTools`). A headless
  `claude -p` cannot answer a permission prompt, so an unapproved
  `submit_brief` just stalls.
- **The person is logged on**, when the hippocampus runs as LocalSystem. It
  starts the command in their logged-on session - their PATH, their profile,
  no stored password. Logged out, the ripple says so, and the brief gate asks
  again later.

**Whose account.** `run_as` is the account the command runs as
(`seren_sinew.runas`): a root service drops to it with `runuser`, a
LocalSystem one borrows the person's logged-on session, and the same account
runs it directly. A root or LocalSystem hippocampus with **no** `run_as`
refuses to run the command - a config edit must not be a root shell. The
Starwright cards fill `run_as` with whoever runs the install.

Starwright flags: `--ripple script|endpoint|off` (a dropdown in the TUI),
`--ripple-command`, `--ripple-url`, `--ripple-token`, `--ripple-run-as`,
`--ripple-stdin`, `--ripple-claude`.

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

AGPL-3.0-only. See `LICENSE`.
