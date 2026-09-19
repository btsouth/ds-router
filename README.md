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
git clone https://github.com/btsouth/ds-router ~/Projects/ds-router
cd ~/Projects/ds-router
./install.sh --dry-run    # see exactly what it would do, change nothing
./install.sh              # do it
```

Any clone location works — the installer rewrites the systemd units and the
manifest to wherever you put it, so `~/ds-router` or a nested path is fine.

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

### Do you need all four providers?

No. Start with the one you have. On a single-provider setup ds-router correctly
concludes there is nowhere better to move anything and leaves every session exactly
where it is — so it is inert until you add a second provider, and adding one is
just another entry in `config.yaml`.

It becomes useful at two: that is when "which one still has quota" stops having an
obvious answer.

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
| `./placement.py --plan --gateway <url>` | Plan against a gated backend, signing in as a browser does |
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
state, not a global config write. Real output from a 27-session fleet:

```
  alias       : deepseek-v4.1-flash
  sessions    : 27 (live backend pid=<pid> port=<port> (serve))
  caps        : ollama-cloud=3
  target      : clinepass=9, commandcode=13, nous=1, ollama-cloud=3, opencode-go=1
  keep 22    move 5
    ollama-cloud   3/3

  session        from           to             model                          why
  ---------------------------------------------------------------------------
  02340f5c       nous           nous           -                              nous is not managed by ds-router; left alone
  036e75ea       ollama-cloud   ollama-cloud   deepseek-v4.1-flash            stays on ollama-cloud (1/3 of its cap)
  418ca809       ollama-cloud   clinepass      cline-pass/deepseek-v4.1-flash moved to clinepass: ollama-cloud is over its concurrency cap
```

Design rules, all asserted by tests:

- **Never assign more sessions to a provider than its cap allows**, counting the
  sessions already active on it that are not part of the plan (read from Hermes'
  own turn leases, so work started outside ds-router still holds a slot). If that
  reading is unavailable the planner says so and charges nobody for it, rather
  than pretending the provider is idle.
- **A cap that cannot be read is not "no cap".** `caps: {ollama-cloud: three}`, or
  a nested `{limit: 3}` where a number belongs, stops the plan with exit 7 and
  names the entry. Reading it as unlimited would fill a provider past a limit it
  really has, which is the queueing this exists to prevent. A quoted `'3'` is read
  as 3 everywhere; before, it crashed the scorer while the planner enforced it and
  the over-cap line ignored it. `caps: 3` instead of a mapping is diagnosed too.
- **Leave sessions alone unless there is a reason.** Moving a session resets its
  prompt cache, so a session only moves when its provider is over cap, hard
  exhausted, or cannot serve your chosen model. 22 of 27 stayed put above.
  A provider whose *usage endpoint* fails is not a reason to move: that is a
  telemetry outage, not a broken endpoint, so a session already there stays. It
  does stop the provider being chosen as a *destination* until it reads again.
- **Never send a session to a provider that is about to throttle while a healthier
  one has room.** Destinations are ranked by how many sessions already sit there,
  so that the fleet stays spread, but a provider is held back to a second tier
  when a window that has not reset is already at 85% or is burning toward 100%
  before its own reset. In the output above that is why the overflow lands on
  clinepass rather than on opencode-go, whose weekly window sat at 98%. If every
  provider is in that state the tier is ignored: a drained fleet still gets
  placed, because refusing would strand the sessions.
- **Never send a session to a provider that does not serve your model.**
- **A provider ds-router does not manage is never touched.**
- **Deterministic** — the same fleet always produces the same plan.

It reads sessions from the running Hermes backend when one is available, and
falls back to reading `state.db` read-only when not.

### When it cannot find the backend

Reading `state.db` only gets you a plan. `--apply` needs the running backend,
because changing a live session's provider is a session-scoped RPC, and ids read
from the database are refused (a stored id is not a live session; `--apply --db`
says so up front rather than failing one session at a time).

Discovery looks for a `hermes_cli.main serve`/`dashboard` process with
`HERMES_DASHBOARD_SESSION_TOKEN` in `/proc/<pid>/environ`, which is how the
Desktop app spawns its own backend. A backend bound to a **non-loopback**
address never carries that token, because Hermes engages its ticket-only auth
gate for any non-loopback bind, and that gate refuses the session token by
design. A non-loopback `dashboard.public_url` engages the same gate even for a
loopback bind, which is the trap on a headless box whose dashboard is published
over Tailscale or a reverse proxy.

A backend gated that way is reached the way a browser reaches it: sign in with a
dashboard credential, mint a single-use WS ticket, then present that ticket on the
upgrade. Point the planner at the backend and give it the credential:

```sh
./placement.py --plan --gateway http://192.168.1.88:9119 \
    --gateway-user you \
    --gateway-password-file ~/.hermes/dashboard-lan-password.txt
```

Or put it in `config.yaml`, so the flags are not needed:

```yaml
gateway:
  url: http://192.168.1.88:9119
  username: you
  password_file: ~/.hermes/dashboard-lan-password.txt
```

That file is yours to write, one per dashboard, mode 0600. The reader accepts either
`label: value` or `label value` lines (Hermes' own LAN file uses colons, a
hand-written one often uses spaces), ignores prose and blank lines, and requires a
username as well. If the file names an `origin` that is not the host you pointed at,
the run says so rather than quietly sending the password somewhere else.

`--gateway-password-env SOME_VAR` is the other credential source, and setting both a
file and an env var is refused rather than one silently winning. There is no
`--gateway-password` flag on purpose: a password in argv is readable by every
process on the machine. With no `gateway:` block and no `--gateway`, nothing
changes and discovery stays on the token path described above.

The origin has to be plain `http` on a host and, optionally, a port, which is what a
tailnet or LAN address is: this transport speaks plaintext HTTP and WebSocket.
Several shapes are refused up front with a message saying what to do instead, rather
than accepted and then failing halfway: an `https` dashboard, one served under a URL
prefix, a wildcard address such as `0.0.0.0`, and a URL with a username or password
inside it. All of those need the second-backend recipe below.

Plain http is a real trade-off, stated rather than hidden: the dashboard password,
the session cookie and the ticket cross that segment unencrypted, so use a tailnet
name where you can rather than a shared LAN address. The sign-in also ignores any
`http_proxy` in the environment: a login is one POST with the password in the body,
and a proxy would receive it whole.

Verified against a real gated backend on this machine, read-only: the plan listed 13
open sessions that the token path cannot see at all, and proposed moving one off an
exhausted provider. Nothing was written; the write path over a ticket is not
exercised here.

With no `gateway:` block at all, those sessions are still visible in `state.db`, and
they cannot be steered. With `--gateway` but no usable credential the run refuses
instead of quietly planning from the state store, because a plan that prints while
steering has stopped is worse than no plan.

If you would rather not store a credential at all, the alternative is a second
backend, loopback-bound, alongside whatever serves your dashboard. This is the
arrangement the Desktop app creates for itself, and the tailnet/LAN backend stays
exactly as it was:

```ini
[Service]
User=youruser
Environment=HOME=/home/youruser
Environment=HERMES_HOME=/home/youruser/.hermes
Environment=HERMES_DASHBOARD_PUBLIC_URL=http://127.0.0.1:9118
EnvironmentFile=-/home/youruser/.hermes/.env   # holds HERMES_DASHBOARD_SESSION_TOKEN
ExecStart=/path/to/venv/bin/python -m hermes_cli.main serve --host 127.0.0.1 --port 9118 --skip-build
```

with a stable line in that `.env` so discovery works across restarts:

```sh
HERMES_DASHBOARD_SESSION_TOKEN=<openssl rand -hex 32>
```

Two things not to do. Do not run that backend with `HERMES_DESKTOP=1` to get
past the gate: that flag also makes the process fire cron jobs itself, so a
second scheduler in the same `HERMES_HOME` runs every job twice. And do not bind
the dashboard backend to loopback and expect it to stay reachable: that is a
deliberate change to how you reach it, not a side effect to accept.

## How it decides

Four inputs, in order of authority:

**1. Hard exhaustion — leave immediately.** A window that has actually run out,
or a health probe that fails. No argument, no waiting to see if it recovers.

A quota that *cannot be read* is deliberately not in this list. A failed reading
says the telemetry endpoint did not answer, not that the provider is down, so a
session already there stays where it is — moving it would pay a prompt-cache reset
to avoid an outage that may not exist. What it does block is choosing that provider
for anything new, until it reads again. The same rule applies to the global choice:
a provider whose reading failed is not abandoned, because that would reset the
prompt cache for every conversation on it to avoid an outage that may not exist.
Hard exhaustion and a failed health probe are evidence about the provider, so both
still move traffic away.

A reading that is *incomplete* is treated the same way, and it used to be treated
as healthy. CommandCode's monthly window needs a second endpoint for the period's
spend; when that figure cannot be read, the window is absent rather than zero, and
the provider is not a destination until it reads. The same applies to any provider
that stops reporting one of the windows it normally carries, which is a hole in
the reading rather than a plan with fewer limits. Three rules follow from the same
idea, and each of them was once the opposite:

- a percentage that cannot be trusted (NaN, infinity, negative, a boolean) is
  dropped, never rewritten to `0%`, because `0%` is both the lowest risk and the
  best headroom;
- a window whose reset time is present but unreadable is dropped, rather than
  silently losing its burn-rate rule;
- a reading with nothing recognisable in it is unreadable, not empty.

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
provider is exhausted or failing.

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

### A provider with no usage API

It can be added, but it cannot be scored, so ds-router will not route *to* it. The
router's whole subject is remaining quota, and a provider that will not report its
remaining quota has nothing to contribute to that decision — inventing a number for
it would be worse than leaving it out, because the answer would look authoritative.

That does not make it useless. It works fine as a plain Hermes provider, and as a
**fallback entry** in Hermes' own chain, which is a separate mechanism: put it last
so it only handles traffic when every measurable provider has failed. Add it under
`providers:` with a `base_url` and `key_env`, and set `quota:` to nothing.

Token Harbor was evaluated this way and left out of the router for exactly this
reason, plus a 53s median response time. See `docs/token-harbor.md`.

## Design notes

- **Quota snapshots can be reused** from a cache directory, so the usage endpoints
  are not polled twice for one answer when something else on the machine already
  polls them. Entirely optional and off in practice for a fresh install: with no
  such directory present, ds-router polls the endpoints directly. The shipped
  default points at `~/.local/state/omarchy/ai-usage/` (a dashboard that writes one
  `<provider>-quota.json` per provider); set `routing.collector_state_dir` to your
  own cache, or `reuse_collector_state: false` to always poll.
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
- **A gated backend is reached the way a browser reaches it, and the credential is
  treated as one.** It comes from a 0600 file or an env var, never from argv; a file
  others can read is refused with the `chmod` to run; a login redirect is not
  followed, because following one cannot authenticate anything and could put the
  password on another host; errors name the credential's *source*, and the password
  is scrubbed out of a server's own error text before it can reach a terminal;
  requests ignore any `http_proxy` in the environment; the cookie is held for the
  life of the process because login is rate limited per client IP, and a credential
  already refused is not retried; a ticket is minted per connection because it is
  single-use with a 30 second TTL. An upgrade refusal carries its HTTP status as
  data rather than putting it in a message for a caller to string-match, and it is
  never read as a stale cookie: Hermes closes a WebSocket before accept for several
  reasons that all look like HTTP 403, and the only reliable "your cookie is stale"
  signal is the 401 from the ticket request itself.

## What is verified

Claims in this README that have been measured, rather than reasoned about:

- All four providers' quota endpoints, read live; each returns the windows
  documented above.
- Ollama Cloud Pro's 3-concurrent cap: at 10 simultaneous requests, 2 returned
  HTTP 429 within ~0.15s while the rest queued 40-80 seconds.
- Hermes' fallback chain: a dead primary walks the chain and answers on the next
  provider.
- ClinePass tool calling, streaming, and its usage API on a personal plan.
- `placement.py --plan` against a live desktop backend, and the concurrency
  reading it now uses: `load.py` counted the sessions active on each provider and
  the plan printed them as holding slots.
- Every HTTP failure shape for a quota endpoint (401, 403, 429, 500, a hang,
  HTML, an empty body, a null body) against the real fetch path: each produces a
  stale reading that names the failure and carries no response body.
- The transport contract: a reply with no result is unconfirmed rather than a
  completed move, a JSON-RPC error is never retried, and a fault from before the
  request went out is retried exactly once.
- First use against the real CLI: with an unset `model.provider/default/base_url`
  in a throwaway `HERMES_HOME`, all three of `apply.py <provider>`, `apply.py` and
  `apply.py --off` write and exit 0. The real CLI answers an unset key with the
  notice on stderr and exit 1, which is why the exit status alone cannot mean the
  read failed.
- A partial reading: for each provider, the windows it is expected to report are
  asserted, and a payload missing one is marked incomplete (with the missing and
  the read windows both named) instead of scoring as a plan with fewer limits.
- Two independent audits of this codebase (one hunting swallowed failures, one
  hunting untested failure modes) found ten real defects, all fixed, each with the
  test that would have caught it: an unreadable session store rewriting the whole
  fleet, `apply` reporting success for replies that meant nothing had happened, a
  crafted clone path executing as shell through the uninstall manifest, and a
  reading that could not be trusted scoring as the healthiest provider. Five tests
  that could not fail were rewritten, and the transport and concurrency layers, which
  had no tests at all, now have 18 between them.
- The gated path, against a real gated backend (read-only) and a loopback fake
  (`test_gateway.py`, 41 tests). Live: 13 open sessions read from a backend whose
  token path cannot reach it, and a plan proposing to move one off an exhausted
  provider; nothing was written. Pinned in the suite: a wrong credential refused
  once and not retried, a rate-limited login named as such, a redirect not followed,
  a ticket response carrying no ticket, a 200 login that sets no cookie, a stale
  cookie re-signed-in exactly once, a file other users can read refused, a
  credential whose file names another origin reported, a password in a URL refused
  without being echoed by the refusal, and a password never reaching a message,
  including through a server's own error detail.
- `install.sh` / `uninstall.sh` in an isolated sandbox, including that
  `--dry-run` writes nothing and a re-run is a no-op.

Claims that are reasoned but **not** verified end to end are flagged inline.

## Requirements

- Python 3.10+ with `pyyaml` (the suite is green on 3.10, 3.11 and 3.12)
- The `hermes` CLI on PATH
- Linux (systemd user units) or macOS (installer prints the launchd/cron
  equivalent; the router itself is pure Python and POSIX sh)

## Limits

- **Selection is per-session, not per-request.** The router chooses a provider
  for a Hermes session; it is not a proxy sitting in the request path. Hermes'
  own fallback chain handles mid-turn failures.
- **`placement.py --plan` is verified against a live fleet of 25+ sessions. The
  write path is verified for the create-time case, and only partly for the move
  case.** Measured against a real backend with throwaway sessions:

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
  a workaround. A second shape is measured there too: when every provider in
  Hermes' chain is spent, a turn hangs rather than failing fast, which is
  indistinguishable from work in progress. Keep a last-resort fallback entry that
  is not metered, so an exhausted fleet degrades to a slower answer instead of a
  silence.
- **Stickiness does not survive a restart.** ds-router re-picks after a reboot,
  which is correct but means the first turn of the first session may move.

## Licence

MIT
