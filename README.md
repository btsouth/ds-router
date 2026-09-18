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
| `python3 test_routing.py` | Run the tests |

Every command that changes something has a read-only counterpart. `--show`,
`--check`, and `--dry-run` never write.

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

## Requirements

- Python 3.10+ with `pyyaml`
- The `hermes` CLI on PATH
- Linux (systemd user units) or macOS (installer prints the launchd/cron
  equivalent; the router itself is pure Python and POSIX sh)

## Limits

- **Selection is per-session, not per-request.** The router chooses a provider
  for a Hermes session; it is not a proxy sitting in the request path. Hermes'
  own fallback chain handles mid-turn failures.
- **A hang is not bounded by ds-router.** A provider that accepts a connection
  and never replies stalls that request until the SDK's own timeout. Health
  probing catches a *dead* provider before you are routed to it, but cannot
  interrupt one that dies mid-turn.
- **Stickiness does not survive a restart.** ds-router re-picks after a reboot,
  which is correct but means the first turn of the first session may move.

## Licence

MIT
