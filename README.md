# ds-router

Keeps your Hermes agent on whichever LLM provider still has quota.

You have several subscriptions that all serve the same open models. Each one has
its own rolling usage windows that throttle you at different times. ds-router
reads every provider's live quota, works out which ones are about to run out,
and points Hermes at a healthy one.

Works with any providers that expose a usage API. Shipped with support for four:

| provider | plan | price | quota windows |
|---|---|---|---|
| CommandCode | GOAT | $10/mo | $14 / 5h, $35 / week, $70 / month |
| OpenCode Go | Go | $10/mo | session, weekly, monthly |
| Ollama Cloud | Pro | $20/mo | $60 / month credits |
| ClinePass | Agent/Pass | $9.99/mo | 5-hour, weekly, monthly |

Total: about $40-50/month for roughly $190+ of metered model access, and the
router is what lets you actually use all of it instead of hammering one
subscription until it throttles you.

## Quick start

```sh
git clone <this repo> ~/Projects/ds-router
cd ~/Projects/ds-router
./install.sh --dry-run    # see exactly what it would do, change nothing
./install.sh              # do it
```

`install.sh` checks your prerequisites, runs the test suite as a preflight,
installs a systemd timer (Linux) or prints the macOS equivalent, enables it, and
verifies the result. **It does not touch your Hermes config** — installing and
routing are separate steps.

Preview what the router would choose before enabling anything:

```sh
./ds-switch --show        # writes nothing
```

Then turn routing on:

```sh
./ds-switch               # apply the router's recommendation now
```

From then on the timer re-checks every 15 minutes. To stop it:

```sh
./uninstall.sh --dry-run  # see what it would remove
./uninstall.sh            # remove the timer
./ds-switch --off         # hand Hermes back to a plain provider
```

## Before you start: add your keys

ds-router reads provider API keys from `~/.hermes/.env`, the same file Hermes
uses. It does not store credentials itself and never writes them anywhere.

```sh
# ~/.hermes/.env
COMMANDCODE_API_KEY=...
OPENCODE_GO_API_KEY=...
OLLAMA_API_KEY=...
HERMES_CUSTOM_CLINEPASS_API_KEY=...
```

Only the providers you actually have need entries. Delete the others from
`config.yaml` and ds-router will ignore them.

## Commands

| command | what it does |
|---|---|
| `./ds-switch` | Apply the router's current recommendation |
| `./ds-switch --show` | Print the decision, write nothing |
| `./ds-switch --check` | Verify the config can actually drive Hermes |
| `./ds-switch <provider>` | Pin one provider, ignoring the router |
| `./ds-switch --off` | Hand Hermes back to `default_provider` |
| `./router.py --dry-run` | Read live quotas, print the decision table |
| `./router.py --dry-run --health` | Also probe each provider with a real request |
| `./router.py --dry-run --json` | Machine-readable output |
| `./router.py --list-models` | Fetch each provider's live catalog |
| `./placement.py` | Spread your open sessions across providers (see below) |
| `python3 run_tests.py` | Run every test suite |

Every command that changes something has a read-only counterpart. `--show`,
`--check`, `--dry-run`, and `--plan` never write.

## Spreading many sessions

`ds-switch` sets one provider for Hermes, so every session you open inherits it.
If you run several agents at once, they all land on the same provider — and
providers cap concurrency. Ollama Cloud Pro allows 3 simultaneous requests, so a
4th session queues and a full queue is rejected.

`placement.py` fixes that by giving sessions *different* providers:

```sh
./placement.py --plan          # show the target spread, write nothing
./placement.py --apply         # do it
```

It uses Hermes' own per-session provider override, so this is session-scoped
state, not a global config write. Real output from a 21-session fleet:

```
  alias       : deepseek-v4.1-flash
  sessions    : 21 (live backend pid=<pid> port=<port> (serve))
  caps        : ollama-cloud=3
  target      : clinepass=4, commandcode=10, ollama-cloud=3, opencode-go=3
  keep 16    move 5
    ollama-cloud   3/3

  session        from           to             model                          why
  ---------------------------------------------------------------------------
  02340f5c       nous           nous           -                              nous is not managed by ds-router; left alone
  036e75ea       ollama-cloud   ollama-cloud   deepseek-v4.1-flash            stays on ollama-cloud (1/3 of its cap)
  418ca809       ollama-cloud   opencode-go    deepseek-v4.1-flash            moved to opencode-go: ollama-cloud is over its concurrency cap
```

Design rules, all asserted by tests:

- **Never assign more sessions to a provider than its cap allows**, counting the
  sessions already there that are not part of the plan. Note that a provider can
  still be *running* over its cap if something outside ds-router started work;
  the planner will not add to it.
- **Leave sessions alone unless there is a reason.** Moving a session resets its
  prompt cache, so a session only moves when its provider is over cap, hard
  exhausted, or cannot serve your chosen model. 16 of 21 stayed put above.
  A provider whose *usage endpoint* fails is not a reason to move: that is a
  telemetry outage, not a broken endpoint, so a session already there stays. It
  does stop the provider being chosen as a *destination* until it reads again.
- **Never send a session to a provider that does not serve your model.**
- **A provider ds-router does not manage is never touched.**
- **Deterministic** — the same fleet always produces the same plan.

It reads sessions from the running Hermes backend when one is available, and
falls back to reading `state.db` read-only when not.

## How it decides

Four inputs, in order of authority:

**1. Hard exhaustion — leave immediately.** A window that has actually run out,
a quota that cannot be read, or a health probe that fails. No argument, no
waiting to see if it recovers.

**2. Burn rate, not raw percent.** A window is only dangerous if it will run out
before it refills, so usage is compared against how much of the window has
elapsed. `47% of a 5-hour window` and `45% of a month` are not comparable as raw
numbers; as burn rates they are. Elapsed time is floored at 15% of a window so a
window that just reset cannot project its first burst into a fake emergency.

**3. Concurrency caps.** A cap applies to requests *in flight*, not to sessions
that merely exist, so this is measured from Hermes' own session store. Ollama
Cloud Pro allows 3 concurrent requests — measured, exceeding it gets you HTTP
429 in about 0.15s while the rest queue for 40-80 seconds. Being over a cap
raises a provider's cost without disqualifying it, because queueing still works.

**4. Stay put unless there is a reason.** Switching providers resets the
upstream prompt cache and drops the model's reasoning traces. Your conversation
is preserved — the router is stateless and Hermes replays the full history — but
those two costs are real, so a conversation keeps its provider until that
provider is exhausted, unreadable, or failing.

Peak pricing is applied as a tie-break only. Both providers charge double during
their own peak windows, and the windows barely overlap:

- **CommandCode**: 01:00-04:00 and 06:00-10:00 UTC, Mon-Fri (note the 04:00-06:00
  gap — it is off-peak there)
- **Ollama Cloud**: 12:00-18:00 UTC, Mon-Fri

So between 12:00 and 18:00 UTC Ollama pays double and CommandCode does not, and
from 01:00 to 10:00 the reverse. Preferring the off-peak provider is free: no
cache reset, no quality change, no context loss.

These hours are read from each provider's published pricing and hardcoded in
`config.yaml` under `peak:`. If a provider changes its schedule the router
silently optimises against the old one — harmless when wrong (it just picks by
headroom instead) but worth re-checking occasionally.

## Changing models

`config.yaml` is the single source of truth. Hermes asks for a logical alias; the
router translates it to each provider's ID:

```yaml
default_model: deepseek-v4.1-flash

models:
  deepseek-v4.1-flash:
    commandcode: deepseek/deepseek-v4.1-flash
    opencode-go: deepseek-v4.1-flash
    ollama-cloud: deepseek-v4.1-flash
    clinepass: cline-pass/deepseek-v4.1-flash
```

Adding a new model is one entry in that table. A model only some providers serve
is fine — the router picks among those that list it, and a provider that does not
serve your chosen alias is skipped rather than sent a request it will reject.

Use `./router.py --list-models` to get real IDs before adding one.

## Adding a provider

1. Add its endpoint and key env var under `providers:`
2. Add its model IDs under `models:`
3. Add a quota reader in `quota.py` if it exposes a usage API

If a provider has no usage API, ds-router cannot score it, and it will be treated
as unreadable. That is deliberate: Token Harbor was evaluated and rejected for
exactly this (dashboard-only allowance) plus a 53s median response time.

## Design notes

- **Quota snapshots are reused** from `~/.local/state/omarchy/ai-usage/` when
  fresh, so the endpoints are not polled twice for one answer. Entirely optional;
  with no snapshot present it polls directly. Set `reuse_collector_state: false`
  to always poll.
- **Failures are honest.** An unreadable quota sorts below a real reading rather
  than being silently trusted, and "everything is exhausted" is reported instead
  of papered over. When nothing is safely readable it degrades to the best
  available provider rather than refusing to choose — being wrong is recoverable,
  stalling is not.
- **Health probes use a realistic token budget.** At `max_tokens: 1` at least one
  provider answers HTTP 500 where others return a truncated success, which
  reported a healthy provider as dead.
- **No response bodies in errors.** Quota failures surface a type and message
  only, never a body that could carry credential-bearing fields.
- **`x-opencode-session`** is forwarded upstream, or OpenCode Go rejects the
  request with `400 MissingSessionID`.

## What is verified

Claims in this README that have been measured, rather than reasoned about:

- All four providers' quota endpoints, read live; each returns the windows
  documented above.
- Ollama Cloud Pro's 3-concurrent cap: at 10 simultaneous requests, 2 returned
  HTTP 429 within ~0.15s while the rest queued 40-80 seconds.
- Hermes' fallback chain: a dead primary walks the chain and answers on the next
  provider.
- ClinePass tool calling, streaming, and its usage API on a personal plan.
- `placement.py --plan` against a live 21-session desktop backend.
- Three critical failures found by an independent audit and fixed: an unreadable
  session store rewriting the whole fleet, `apply` reporting success for replies
  that mean nothing happened, and a crafted clone path executing as shell through
  the uninstall manifest.
- `install.sh` / `uninstall.sh` in an isolated sandbox, including that
  `--dry-run` writes nothing and a re-run is a no-op.

Claims that are reasoned but **not** verified end to end are flagged inline.

## Requirements

- Python 3.10+ with `pyyaml`
- The `hermes` CLI on PATH
- Linux (systemd user units) or macOS (installer prints the launchd/cron
  equivalent; the router itself is pure Python and POSIX sh)

## Limits

- **Selection is per-session, not per-request.** The router chooses a provider
  for a Hermes session; it is not a proxy sitting in the request path. Hermes'
  own fallback chain handles mid-turn failures.
- **`placement.py --plan` is verified against a live 21-session fleet. The write
  path is verified for the create-time case, and only partly for the move case.**
  Measured against a real backend with throwaway sessions:

  | case | what happens |
  |---|---|
  | `session.create` with `{model, provider}` | **Works.** The session is created on that provider; `info.provider` reports it and the stored `model_config.provider` agrees. This is the reliable path. |
  | `config.set` on a **idle** session | Applies immediately; the RPC confirms `scope: "session"`. |
  | `config.set` on a session **mid-turn** | Deliberately deferred: the backend stashes the pick and applies it at the next turn start. The change is not lost, it is late. |

  That deferral matters here, because over-cap providers are disproportionately
  the *busy* ones — a provider is over its concurrency cap precisely because
  sessions are running on it — so some of the moves the planner wants are exactly
  the ones that land a turn later. Nothing is lost; the spread completes as those
  turns finish. The dependable behaviour is **choosing a provider when a session
  starts**, which is the concurrency fix itself.

  A move can also be **refused**: Hermes asks for confirmation before abandoning a
  large cached context (>= `model.switch_context_confirm_tokens`, 100k by default)
  when the destination uses a different model id. That fires for cross-provider
  moves, since `cline-pass/...` and `deepseek/...` are not the same string as
  `deepseek-v4.1-flash`. `placement.py` is non-interactive and its intent is
  unambiguous, so it retries once with the backend's confirmation flag rather than
  reporting a move that did not happen.
- **A hang is not bounded by ds-router.** Health probing catches a *dead*
  provider before you are routed to it, but nothing here can interrupt one that
  dies mid-turn — only Hermes' own timeout can. There is a Hermes bug in that
  area worth knowing about: per-provider timeout config is silently ignored for
  named custom providers, so the effective bound is 600 s rather than your
  setting. See `docs/hangs-and-timeouts.md` for the measurement, the cause, and
  a workaround.
- **Stickiness does not survive a restart.** ds-router re-picks after a reboot,
  which is correct but means the first turn of the first session may move.

## Licence

MIT
