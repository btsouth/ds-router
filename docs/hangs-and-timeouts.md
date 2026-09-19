# Hangs and timeouts: a Hermes bug worth knowing about

Investigated 2026-09-18 against Hermes v0.21.3 (upstream `cdceca42`). Verified by
reading the code and by live reproduction against a blackhole server (accepts the
TCP connection, never replies).

## What was measured

A provider that accepts a connection and never replies stalls a turn for a long
time. Two config keys exist that look like they should bound this, and neither
does for a **named custom provider**:

```
providers.blackhole.request_timeout_seconds: 60   -> turn still ran 400s+
providers.blackhole.stale_timeout_seconds: 45     -> turn still ran 420s
```

But the config itself is read correctly:

```
get_provider_request_timeout("blackhole") -> 60.0    (what the config holds)
get_provider_request_timeout("custom")    -> None    (what the code asks for)
```

## Root cause: provider-identity mismatch

A named `providers.<name>` entry resolves at runtime to the bare string
`"custom"`, not the config key. Every timeout lookup keys on `agent.provider`,
which holds `"custom"`, so it misses the config entirely and returns `None`.
With `None`, the client is built without an explicit timeout and the configured
value is silently discarded.

Confirmed live: a session using the `clinepass` custom provider records
`billing_provider = custom` in `state.db` while `model_config.provider` holds
`clinepass`. The durable identity exists as `agent.requested_provider`; the
timeout lookups simply do not consult it.

Affected call sites (all miss for a named custom provider):
`agent/agent_init.py:967`, `run_agent.py:557,567,609`,
`agent/chat_completion_helpers.py:620,2748`,
`agent/client_lifecycle.py:476,482`, `agent/agent_runtime_helpers.py:914,1935,1959`.

**The fallback path does not have this bug.** `agent/client_lifecycle.py:69`
passes the raw config key, so fallback entries honour their configured timeouts.
That asymmetry is why the same setting appears to work in some situations.

## What actually bounds a hang today

Not the SDK. The OpenAI SDK's `read=600` default is unreachable on this path
because the per-request timeout is always set explicitly to something else.

| path | bound | why |
|---|---|---|
| streaming, cloud (what `hermes -z` uses) | **600 s** | `_cloud_stale_timeout` = `max(180, reasoning floor)`; `deepseek-v4.1-flash` has a 600 s floor |
| streaming, local endpoint | **900 s** | `local_stream_stale_timeout` default |
| non-streaming | **up to 5400 s** | `HERMES_API_TIMEOUT` 1800 s x `api_max_retries` 3 |

So a hang is bounded, but not at any value you configured, and 600 s is a long
time to wait in an agent loop.

**The 600 s turn-liveness watchdog does not help here.** During a live hang the
streaming monitor refreshed the activity clock every 30 s, so the liveness
threshold never fired.

## Workaround

Write the timeout under the canonical key the runtime actually uses:

```sh
hermes config set providers.custom.request_timeout_seconds 300
hermes config set providers.custom.stale_timeout_seconds 300
```

Verified: with `providers.custom.stale_timeout_seconds: 8`, a blackhole aborted
after 5 stale attempts with a clear message instead of running 1800 s+.

### Two things to weigh before setting it

**1. `custom` is shared.** Every named custom provider resolves to the same
identity, so this key applies to all of them at once, not per-name. With both
`clinepass` and `tokenharbor` configured, one value covers both.

**2. Do not set it aggressively.** The stale timeout means "no output for N
seconds", and reasoning models legitimately think for a while before emitting
the first token — that is why the cloud path carries a 600 s floor for
`deepseek-v4.1-flash`. A value below roughly 120 s risks killing healthy slow
reasoning and turning a working provider into a flapping one. The point of the
workaround is to replace an unknown bound with a known one, not to make it tight.

## Upstream fix (not ours to make)

Thread `agent.requested_provider` into the lookups: add an optional
`fallback_provider_id` to `hermes_cli/timeouts.py::_configured_timeout`, retry
the lookup with it on a miss, and pass it at the call sites above.
`agent/client_lifecycle.py:701` already does exactly this for the `key_env`
lookup and is the in-repo precedent. Verified in-memory against a blackhole:
the hang stopped at 8.1 s instead of 1800 s+ once the identity was threaded.

This affects anyone using a named custom provider, so it is worth reporting
rather than working around forever.

## A second hang shape: when there is nothing left to fall back to

Measured 2026-09-19 on Hermes v0.21.3, while verifying a new fallback entry.

Setup: a throwaway `HERMES_HOME` whose primary was `opencode-go` (sitting on its
weekly limit) and whose only fallback entry was also `opencode-go`.

```
hermes -z "Reply with exactly: fallback ok"    -> no output, no error, no usage file
                                                  killed by `timeout 240` (exit 143)
```

The same scratch home with a healthy fallback entry added answered in about 20
seconds and recorded `provider: deepseek` in its `--usage-file`. So the difference
is not the request shape or the model: it is whether anything in the chain can
answer at all.

This is not a network stall. The exhausted provider refuses in ~0.15 s (a 429 with
`Weekly usage limit reached`), so the 240 s went somewhere in the retry path rather
than waiting on a socket. That last step is an inference from the timings, not a
measurement: the retry count was not instrumented.

Why it matters here: a hang is indistinguishable from work in progress, to a human
watching a terminal and to a cron job, so an exhausted fleet can look busy for
hours. ds-router's premise is to keep a provider in the chain that has measured
headroom, and this is the shape it exists to prevent. But the last mile is the
fallback chain, and a chain whose entries are all metered can end up with nothing
to answer. A last-resort entry that is not metered (an API key with a balance
behind it) degrades that case to a slower answer instead of a silent hang.

## What this means for ds-router

The health probe is the mitigation that already exists here: it sends a real
request to each provider before routing to it, so a provider that is *dead* gets
excluded before you depend on it. What neither the probe nor ds-router can do is
interrupt a provider that dies *during* a turn — only Hermes' own timeout can,
and that is the bug above.
