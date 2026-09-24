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

## The two loops

- **sleep** (~20 h, deliberately not 24): purge flagged, take the brief (or
  pull one from recent short-terms with the small model), group short-terms
  by topic, show the model each cluster with the nearest existing cores, and
  submit one docket for the sleep. Short-terms a pending docket already holds
  are left alone. Then the mechanical tidy: age out, maintain near-term,
  sweep pruned.
- **tend** (every few minutes): reviewed dockets with denied operations and
  no later attempt in their chain get redrafted and resubmitted.

`sleep.mode: thread` runs both here; `external` means something else
POSTs `/sleep` and `/tend` on a schedule.

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

The config has four blocks: `server` (this service's bind and bearer),
`memory` (where SerenMemory is and the bearer to present to it - the same
three pointers as every server block), `model` (the small model's
OpenAI-compatible endpoint), `sleep` (intervals, thresholds, attempts).
Beyond loopback with no bearer the service refuses to start, like every
Seren service.

## API

| route          | what                                                  |
|----------------|-------------------------------------------------------|
| `GET /`        | service, version, mode, whether a model is configured |
| `GET /health`  | liveness, and whether Memory answers                  |
| `GET /status`  | last sleep and last tend, intervals                   |
| `POST /sleep`  | run a sleep now (409 if one is running)               |
| `POST /tend`   | pick up denied operations now                         |

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
