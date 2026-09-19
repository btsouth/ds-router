# Per-session routing vs global routing

This is the most consequential design fact in the project. It was established by
reading the Hermes source, and it changes what ds-router should do.

## The finding

**Hermes supports a per-session provider override. The `ds-switch` approach
(mutating global `model.provider`) is the wrong tool for multi-session use.**

Evidence:

- `tui_gateway/methods_session.py:306` — `_create_overrides(params)` builds a
  per-session override from the `model` and `provider` params of
  `session.create`:
  ```python
  create_model = _str_param(params, "model")
  if create_model:
      model_override = {"model": create_model, "provider": _str_param(params, "provider") or None}
  ```
  Its docstring is explicit: *"PER-SESSION (model, reasoning, service_tier)
  overrides from the composer — never a global config write."*

- `tui_gateway/model_switch.py:290` — `/model` without `--global` writes
  `session["model_override"]`, deliberately *not* process-global env, because
  *"the desktop hosts every same-profile session in one process, so
  os.environ would leak the switch to all."*

- `gateway/run_turn.py:192` — the override is applied per turn, so it persists
  across turns and survives resume.

So N concurrent sessions can each hold a different provider simultaneously. That
is exactly what the concurrency problem needs.

## Why it matters

Measured on this machine: 12 sessions open and active, 8 of them on
ollama-cloud. Ollama Cloud **Pro caps concurrent requests at 3**; requests
beyond the cap are queued, and a full queue is rejected. So 8 sessions on Ollama
is a stall or rejection risk, not a theoretical one.

A global `model.provider` write cannot fix this, because it is one value for
every session. Worse, a global write mid-flight rebuilds every session's agent
on its next turn.

## Verified: it is fully drivable from a script (2026-09-17)

A probe against the running desktop backend (throwaway sessions only; config
hash unchanged, probe sessions deleted) confirmed **three programmatic routes**,
none needing a human to type `/model`:

| Route | Call | Notes |
|---|---|---|
| Assign a live session | `config.set {session_id, key:"model", value:"<model> --provider <p> --session"}` | What the desktop composer itself calls; returns `scope: "session"`. Requires a live session (else error 4001). |
| Create on a provider | `session.create {model, provider}` | Two sessions were pinned to different providers **simultaneously in one process**, each reporting its own. |
| Slash passthrough | `slash.exec {session_id, command:"/model <m> --provider <p>"}` | Applies to the live agent immediately. |

Transport is JSON-RPC over the backend's WebSocket (`/api/ws`), authenticated
with the per-spawn `HERMES_DASHBOARD_SESSION_TOKEN`, readable from the backend
process's `/proc/<pid>/environ`.

So per-session placement works for arbitrarily many concurrent sessions. **This
is the real mechanism for spreading N agents across providers**, and it means the
concurrency cap can be enforced by placement rather than only by scoring.

The earlier note that the desktop composer does not send `model`/`provider` is
still true of the *UI* (`create-overrides.ts` forwards only title, reasoning,
seed messages) — the *RPC* accepts them. Capability present, UI not surfacing it.

## Corrected: what a global config write does to open sessions

An earlier version of this doc implied a `model.provider` change only affects new
sessions. That is wrong. `tui_gateway/model_switch.py:367` `_sync_agent_model_with_config`
runs at **every turn start**:

- **Unpinned session** (never ran `/model`, no persisted provider) → **adopts the
  config change on its next turn**, no restart.
- **Pinned session** → keeps its provider. The function returns immediately when
  `session["model_override"]` is set.

Confirmed live: with config on `commandcode`, 9 open sessions ran commandcode
(unpinned) while 8 still ran ollama-cloud (pinned via a persisted provider).

**Consequence for `ds-switch`:** rewriting `model.provider` moves every
*unpinned* open session on its next turn. The sticky guard keeps this rare, but
the blast radius is real and worth knowing before it fires.

## Options this opens, in order of fit

1. **Balance on creation.** Choose a provider when a session starts, so the
   spread is intentional rather than accidental. Needs a client change or a
   wrapper.
2. **Cap the per-provider session count.** Treat Ollama's 3-concurrent limit as
   a hard constraint on how many sessions may sit on it, the way `skip_at`
   treats a spent window.
3. **Keep ds-router for the primary only.** Use it to pick what *new* sessions
   default to, and accept that it cannot shape N existing sessions.

## Open questions, answered since

- **Is queue-then-reject observable as a status?** Yes: HTTP 429, measured at
  ~0.15 s when a provider is over its cap, while the rest queue 40-80 s. The cap is
  still a hard limit: the planner never assigns more sessions to a provider than it
  allows, and it counts the requests that provider is already running before it
  does, so what it refuses is filling the queue rather than the cap.
- **Do CommandCode and OpenCode Go have comparable caps?** Only Ollama documents
  one (3 concurrent), so `config.yaml` declares a cap only where one is published.
