# Token Harbor as a fourth source

Evaluated 2026-09-17. **Verdict: works, but it does not fit the router, and it is
a poor fifth provider unless you change what you want from it.**

## What it is

An OpenAI-compatible gateway (one key, many models) at
`https://tokenharbor.ai/v1`. Configured here as a named custom provider
`tokenharbor` with `key_env: HERMES_CUSTOM_TOKENHARBOR_API_KEY`.

## What works (all verified live)

- `/v1/models` returns 35 models with per-model pricing and capability flags.
- Chat completions work on `deepseek-v4.1-flash`, `deepseek-v4-flash`, and others.
- **Tool calling works** — returned a well-formed `tool_calls` array with
  `finish_reason: tool_calls`.
- **Streaming works** — proper `data:` SSE chunks including `reasoning_content`.
- `reasoning_content` is returned as a separate field, same shape as CommandCode.

## The Agent Pass, precisely

$1.99/month (first month $0.99), giving **$10 of included usage**, with a
"model boost" on selected models up to $20. The boost currently applies a 2x
multiplier to DeepSeek V4.1 Flash through September 23, 2026.

Structure: the four-week allowance is split into **four 7-day windows**, one
quarter each, and **unused room does not carry forward**. So the practical
budget is about $2.50/week, or up to $5/week while the 2x boost lasts.

At list price for `deepseek-v4.1-flash` ($0.30 in / $1.20 out), $10 is roughly
2 million input tokens at list — but the boost doubles the reach on that model
for now.

## Why it does not fit ds-router

**There is no usage API. Confirmed by their own documentation.** The
docs index lists every topic Token Harbor documents, and it contains an API
section with exactly three pages: Chat APIs, Models, and Prompt caching. There
is no usage, allowance, or quota endpoint documented anywhere.

The Universal Key page states it outright: *"There is no key-management API you
can drive with a Universal Key — no `/v1/keys/...` endpoints. Creating, rotating
and revoking keys is done while signed in to the dashboard."* The dashboard
requires an interactive login; it is not reachable with the API key.

Every reference to usage in their docs points at the web page
`/dashboard/usage` ("your last 100 requests with tokens, cache layer, and exact
cost"), never an API route.

Probed and 404: `/usage`, `/account`, `/billing`, `/subscription`, `/limits`,
`/me`, `/credits`, `/pass`, `/v1/dashboard/usage`, `/v1/key`, `/v1/auth/me`,
`/v1/pass/usage`, `/v1/allowance`, plus `/api/v1/*` and `api.tokenharbor.ai/*`
(that subdomain is a hosting catch-all returning "Application not found", not
their API). No rate-limit or quota headers are returned on completions either.

So the allowance bar on the dashboard is real but **web-only**. Reading it would
require a logged-in browser session, not the API key — which rules it out for
the usage dashboard and for quota-aware routing.

The router's entire premise is reading a provider's quota and scoring remaining
headroom. Token Harbor exposes no machine-readable allowance, so it cannot be
scored the way the other three are. It would have to be either always-on or
always-off, or scored from locally counted tokens (which cannot see usage from
any other client).

## The real risk: latency variance

Same tiny prompt (`"Say hi"`, `max_tokens: 5`), eight samples:

```
0.88s  27.24s  44.94s  50.72s  53.32s  53.35s  71.49s  73.17s
min 0.88s   median 53.32s   max 73.17s   ratio 83x
```

**Median 53 seconds.** Seven of eight samples took 27s or longer; exactly one
came back under a second. For comparison, measured the same way in one batch:
commandcode 1.06s, ollama-cloud 1.03s, opencode-go 0.23s.

This is not marginal degradation — it is unusable for an agent loop where one
user turn may make 10+ API calls. At a 53s median, a single turn would take
nine minutes of waiting.

The cause is not diagnosed. It is steady enough across samples (53.32 and 53.35
are suspiciously close) to suggest a fixed server-side timeout or a
parked-request pattern rather than random congestion, but that is speculation.
Either way it cannot be predicted or worked around from the client side.

**Conclusion: Token Harbor is not usable as a router target or a fallback.** The
one sub-1s sample shows the gateway *can* be fast, so this may be launch-period
capacity rather than a permanent property. Worth re-testing in a week or two;
not worth wiring up now.

## Recommendation

**Do not add it to the router or the fallback chain yet.** Reasons, in order:

1. No quota API, so it cannot be scored — it would be a hardcoded always/never.
2. Intermittent 40-50s stalls make it unfit as a fallback, where the whole
   point is being available when something else fails.
3. The allowance is small ($2.50/week effective) relative to a coding-agent
   workload, so even a perfect integration buys little.

**Where it could earn a place:** as an explicit, manual option for cheap
non-urgent work — the free tier, or the Agent Pass for batch jobs that tolerate
latency. Select it with `/model` when you want it, rather than routing to it
automatically.

If the latency variance turns out to be transient (a launch-period capacity
issue), re-test in a week; a provider that answers reliably in under 5s would be
a reasonable last-resort fallback entry.

## Documented rate limits

From the Universal Key page: *"We auto-rate-limit bursts above 30 req/s/IP
across all unauthenticated and authenticated traffic."* That is the only
published rate limit, and it is per-IP, not per-plan. No concurrency limit is
documented.

## Open items

- Confirm whether the 2x boost on DeepSeek V4.1 Flash (through Sep 23) is worth
  using deliberately before it expires.
- Free models (`deepseek-v4.1-flash:free`, `mimo-v2.5:free`,
  `deepseek-v4-flash:free`) returned `403 free_models_disabled` — requires
  opting in via the dashboard's free-model consent.
