# Candidate providers: verdicts

Evaluated against a single bar: can this serve as a Hermes provider, and does it
fit the router (quota API, reliability, terms).

Checked 2026-09-17. Prices and policies change; re-verify before subscribing.

## Summary

| Provider | Price | Verdict | Reason |
|---|---|---|---|
| **ClinePass** | $9.99/mo | **Worth adding** | Third-party agent use explicitly documented; quota API exists |
| **DevPass** | $29-179/mo | **Avoid** | ToS bans cron/automation from Oct 15 2026; ban without refund |
| **CheapestInference** | $17.99+/block | **Avoid** | Per-request cap may be 1 MB vs your 1.5 MB peak; 1 concurrent request; discretionary fair-use throttle |
| **Token Harbor** | $1.99/mo | **Manual only** | Median 53s latency; no usage API (see token-harbor.md) |

## ClinePass — $9.99/month

A flat-rate subscription from Cline Bot Inc. bundling curated open-weight coding
models (DeepSeek V4 Pro/Flash, GLM-5.x, Kimi K2.6/K2.7/K3, MiniMax M3, MiMo
V2.5, Qwen3.7/3.8), billed as "2-5x the usage on popular open coding models
compared to standard API rate".

- **Base URL:** `https://api.cline.bot/api/v1` (OpenAI-compatible chat completions)
- **Auth:** static API key as `Authorization: Bearer $CLINE_API_KEY`
- **Model IDs:** `cline-pass/<slug>`, e.g. `cline-pass/deepseek-v4-flash`.
  Documented, but **no `/v1/models` endpoint is documented** — the list has to
  be maintained by hand.
- **Tools + streaming:** yes, OpenAI function-calling format and SSE.

**Why it is worth adding: their docs explicitly invite this use.**
Verbatim, from the ClinePass docs page:

> "Using ClinePass outside of Cline — You can use ClinePass models from your own
> scripts, apps, or automation through the Cline API."

And the FAQ: *"Can I use ClinePass with other coding agents? Yes, you can use
ClinePass with other coding agents by using your ClinePass API key."*

That is the opposite of DevPass's stance, and it is why ClinePass is the clean
add.

**Quota:** three windows (5-hour rolling, weekly, monthly) but **no numeric
limits are published** — quota is expressed only as a multiple of provider list
rates. A usage API does exist on the Enterprise surface:
`GET /api/v1/users/me`, `/api/v1/users/{id}/balance`, `/api/v1/users/{id}/usages`.
Worth probing with a real key before assuming it works on a personal
subscription, since the docs present it as Enterprise.

**Caveats:** no published RPM or concurrency ceiling; the ToS requires written
consent to transfer/resell API keys (§2.2(4)) and restricts automated access
that exceeds human browsing levels (§2.2(2)) — the latter plainly targets
scraping the web service, not a purchased API plan.

## DevPass (LLM Gateway) — $29/79/179 per month

A flat-rate pass on LLM Gateway's OpenAI-compatible endpoint
(`https://api.llmgateway.io/v1`), 200+ models including `deepseek-v4.1-flash`,
with a genuinely good quota API (`GET /v1/key` returns plan, credits used, limit,
remaining, and premium-window fields, explicitly so key-only clients can read
usage without a dashboard session).

**The plumbing is ideal and Hermes is whitelisted by name** — but the terms
disqualify it for how this agent is used. Verbatim from their terms:

> "DevPass is licensed solely for interactive use through approved coding and
> agent tools. It is **not** a general-purpose API: you may not use your DevPass
> API key to power your own applications, products, services, backends, scripts,
> batch jobs, or any other direct API integration."

> "DevPass is only usable from whitelisted clients such as Claude Code, Codex,
> Cursor, Cline, OpenCode, OpenClaw, **Hermes**, and Autohand."

And the clause that decides it, effective **October 15 2026**:

> "you may not use your DevPass API key to power your own applications,
> products, services, backends, scripts, **cron jobs**, batch pipelines, bots, or
> any other direct or automated API integration, whether or not a coding agent is
> involved"

Enforcement is explicit: *"We may rate-limit, suspend, or downgrade accounts that
show signs of automated abuse, key sharing, resale, or sustained traffic
patterns inconsistent with interactive coding workflows"*, and violations *"can
get your DevPass account banned without a refund."* One account per person, no
team use.

**Why this rules it out here specifically:** this setup runs a cron job that
calls `hermes config set`, background session polling, and multi-agent fan-out.
That is the exact "sustained traffic patterns" and "cron jobs" pattern the terms
prohibit. Adding it as always-on capacity is a terms violation, not a technical
one — the API would work fine right up until the account is banned.

**Note:** DevPass also advertises 120 req/min and 50 concurrency, better
documented than anyone else here. The limits are not the problem; the licence is.

## CheapestInference — $17.99 per 8h block (Core pool)

Unlimited flat-rate blocks rather than metered credits. Three 8-hour windows
(Asia-Pacific 00-08, Europe 08-16, Americas 16-24 UTC); one block $17.99, all
three for 24/7 at $59.97/mo ($15.29/block annually). Core Pool serves exactly
the model in use here: `deepseek-v4.1-flash`, plus `mimo-v2.5`.

- **Base URL:** `https://api.cheapestinference.com/v1`, static `sk-` key.
  Anthropic dialect at `/anthropic`.
- **Verified live:** `/v1/chat/completions`, `/v1/models`, `/v1/usage` all return
  401 (exist, need auth); `/health` returns 200.
- **Quota API: yes.** `GET /v1/usage` with the `sk-` key returns plan slug,
  status, and expiry, no dashboard session needed. `GET /api/usage` with an
  `mk_` management key adds per-key request/token stats. Management endpoint is
  rate-limited to 12 requests per 12 hours, so cache it.
- **Tools + streaming:** documented, including tool calling.

**Three blockers, the first of which is fatal if its docs are right:**

**1. Per-request payload cap conflicts with observed usage, and their own docs
disagree on the number.** The models index says **8 MB**; the
`/docs/models/deepseek-v4-1-flash/` page says **1 MB**. Measured from this
machine's session history, context per API call (input + cache_read, since
cached tokens still travel on the wire):

```
peak context/call: 352,316 tokens  ->  ~1,376 KB text
plus tool schemas (~100 KB)        ->  ~1,476 KB payload
```

Every one of the top 12 sessions exceeds 1 MB. If 1 MB is correct, requests are
rejected with `context_length_exceeded`, and since the client only compacts
*after* that error, the penalty lands exactly when a session is deep in real
work. **Resolve this before paying anything.**

**2. One concurrent request per key.** Stated as fair-use concurrency, and the
pools page repeats it: *"Fair use: one request at a time per key."* Parallel
capacity requires multiple subscriptions combined into one key (capacity stacks
where blocks overlap). For a setup running 12+ concurrent sessions this
multiplies the effective price by 3-4x.

**3. Unpublished discretionary fair-use throttle, no refund.** Verbatim from
their terms §1.2:

> "Unlimited subscriptions are, in addition, subject to a fair-use protection:
> we may apply safeguards to usage patterns that substantially exceed what a
> single subscriber's workload can reasonably generate — such as deliberate
> saturation, runaway automation loops, or use inconsistent with an individual
> subscription. Where a safeguard is triggered, the affected key may be
> throttled or paused for the remainder of the billing period. Safeguards …
> their calibration is internal, may change at any time, and is not published.
> Activation of a fair-use safeguard is not a service failure or outage and does
> not give rise to any refund, credit, or other remedy."

That clause describes this workload: many concurrent sessions, a cron job, and
high daily volume. Also prohibited under §4: reselling access, and exceeding the
per-key concurrency limit.

**No third-party-client prohibition** — the opposite, agents are the marketed
use case, with documented OpenCode config and an MCP server. The problem is not
licensing; it is throughput.

**Signals:** domain registered 2025-09-25 (young), no independent reviews, a
status page at `status.cheapestinference.com` (all operational when checked),
98.8% over 81 HTTP probes per one third-party monitor. Their `/pools` marketing
page still advertises "DeepSeek V4 **Flash**" while docs and the model page say
V4.1 Flash — sloppy copy, and combined with a self-contradicting payload cap it
suggests the docs are not tightly maintained.

**If testing anyway:** one Core block, one month, $17.99, never annual. First
action: send a single request with a ~300k-token context to settle the 1 MB
question. That is an $18 answer to the only question that matters.

## ClinePass status: WIRED IN and verified (2026-09-18)

Both open questions resolved, so this is now an active provider.

**1. The quota API works on a personal subscription.** This was the blocker.
`GET /api/v1/users/me/plan/usage-limits` returns all three windows, same shape
as the other providers:

```json
{"data": {"limits": [
  {"type": "five_hour", "percentUsed": 0, "resetsAt": "2026-09-18T09:06:35Z"},
  {"type": "weekly",    "percentUsed": 0, "resetsAt": "2026-09-25T04:06:35Z"},
  {"type": "monthly",   "percentUsed": 0, "resetsAt": "2026-10-18T04:06:35Z"}
]}}
```

The dashboard shows the same 0/0/0, so the API and the portal agree. Plan reads
as `Cline Pass (Monthly)`. Unlike Token Harbor, a script gets the same numbers a
human sees, which is the property that makes a provider scorable.

**2. Model IDs.** 14 available, all probed live and working:
`cline-pass/` + deepseek-v4.1-flash, deepseek-v4-pro, deepseek-v4-flash, glm-5.3,
glm-5.2, kimi-k3, kimi-k2.7-code, kimi-k2.6, mimo-v2.5, mimo-v2.5-pro,
minimax-m3, qwen3.8-max, qwen3.7-max, qwen3.7-plus.

Note `deepseek-v4.1-flash` **works but is absent from Cline's own docs table**
(which lists only v4-flash and v4-pro). Verified by probing, not by docs.

**3. Gotcha: discovery is actively harmful here.** `/v1/models` returns 446
models with **zero `cline-pass/*` entries** — it serves the full OpenRouter
catalog, none of which the pass covers. Model discovery wrote all 446 into
config and would re-add them on every refresh. Set `discover_models: false` and
keep an explicit list.

**4. The HTTP 500 "empty response content" is a token-budget artifact, not an
outage.** At `max_tokens: 5` ClinePass returns `HTTP 500 {"error":"empty response
content"}` on roughly 2 of 3 requests. This looks alarming in a benchmark.

It is not a ClinePass defect. The same probe at `max_tokens: 5` against
**commandcode and ollama-cloud returns `finish=length` 3/3** — they silently
return truncated output where ClinePass errors. Reasoning models spend the whole
budget on thinking before emitting content, and ClinePass reports that as a 500
rather than as a truncated success. Measured threshold:

| max_tokens | result |
|---|---|
| 5 | 1/3 ok (other 2: HTTP 500) |
| 20 | 3/3 ok |
| 50+ | 3/3 ok |

Hermes uses realistic budgets, so this does not bite in practice: three
consecutive real Hermes turns on ClinePass all completed (13,215 / 13,201 /
13,202 tokens). Worth knowing only because it will make any low-budget
health-check show false failures.

## What to do with ClinePass

It is wired in, and the sections above are the record of why: an Agent Pass is a
plan for an agent, so the terms of use are the real constraint, but its usage API
does report the windows this router needs, and its caps are unstated rather than
absent. Read the sections above before deciding anything about it.
