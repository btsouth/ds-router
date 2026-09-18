# ds-router

Quota-aware provider router for three subscriptions that all serve the same
open models:

| provider | plan | cost | quota windows |
|---|---|---|---|
| CommandCode | GOAT | $10/mo | $14 per 5h, $35 per week, $70/mo (dollar-denominated) |
| Ollama Cloud | Pro | $20/mo | $60/mo credits, no rolling windows |
| OpenCode Go | Go | $10/mo | session, weekly, monthly (percent-denominated) |

## What this is for, and what it is not

All three serve `deepseek-v4.1-flash` at **identical per-token rates**, so there
is nothing to optimise on price per token or model quality. The only job here is
**quota arbitration**: notice which subscription is about to throttle a session
and stay off it.

It is not a general "smart router". It does not pick models by task type, and it
does not chase cents.

## The three ideas that matter

**1. Compare burn rate, not raw percent.** A window is dangerous when it will
run out before it refills, so usage is compared against elapsed window time.
`47% of a 5-hour window` and `45% of a month` are not comparable as raw numbers;
as burn rates they are. Elapsed time is floored at 15% of a window so a window
that just reset cannot extrapolate its first burst into a fake emergency.

**2. Hard exhaustion and soft pressure get opposite responses.** A provider that
has actually run out is left immediately, whatever the cost. A provider that is
merely burning fast is a *prediction*, and a wrong prediction costs a cache
reset, so it is not worth moving for on its own.

**3. Stay put until there is a reason.** Switching providers resets the upstream
prompt cache and drops the model's reasoning traces. Context is preserved (the
router is stateless; Hermes replays the full conversation), but those two costs
are real. So a conversation keeps its provider until that provider is exhausted,
unreadable, or predicted to die mid-turn.

## Peak pricing: the free win

Both providers charge peak rates, at windows that barely overlap:

- CommandCode: 01-04 and 06-10 UTC, Mon-Fri (2x)
- Ollama Cloud: 12-18 UTC, Mon-Fri (2x)

Between 12:00 and 18:00 UTC, Ollama pays double and CommandCode does not; from
01:00 to 10:00 UTC the reverse. Preferring the off-peak provider during those
blocks costs nothing: no cache reset, no quality change, no context loss. It is
strictly an efficiency gain, and it is applied only as a tie-break between two
already-safe providers, never as a reason to abandon a healthy sticky one.

## Changing models

The model table is the entire point of `config.yaml`. Hermes asks for a logical
alias; the router translates it per provider:

```yaml
models:
  deepseek-v4.1-flash:
    commandcode: deepseek/deepseek-v4.1-flash
    opencode-go: deepseek-v4.1-flash
    ollama-cloud: deepseek-v4.1-flash
```

Adding DeepSeek 4.2 when it ships is one entry in that table and nothing else.
A model only some providers serve is fine; the router picks among those that
list it. To find the real ids, use `./router.py --list-models`.

## Usage

```bash
./router.py --dry-run                 # read live quotas, print the decision
./router.py --dry-run --health        # also time a real request per provider
./router.py --dry-run --json          # machine-readable
./router.py --list-models             # fetch each provider's catalog
python3 test_routing.py               # the test suite
```

`--dry-run` proxies nothing. It is the safe way to watch the router think before
trusting it with traffic.

## Design notes

- **Quota snapshots are reused** from `~/.local/state/omarchy/ai-usage/` when
  fresh, because the omarchy-usage-dashboard collector already polls these same
  endpoints. No point hitting a usage API twice for one answer.
- **Failures are honest.** An unreadable quota sorts below a real reading rather
  than being silently trusted, and "all providers exhausted" is reported rather
  than papered over with a guess.
- **No response bodies in errors.** Quota failures surface a type and message
  only, never a body that could carry credential-bearing fields.
- **`x-opencode-session`** must be forwarded upstream or OpenCode Go rejects the
  request with `400 MissingSessionID`. Configured per provider.

## Not done yet

- The HTTP proxy itself. Only the decision layer is built and tested.
- Mid-turn swap on an exhausted provider: the router flags "expected to exhaust
  within Nm" so a caller can act, but nothing consumes that flag yet.
- Persisting stickiness across restarts. A restart re-picks, which is correct
  but means the first turn after a restart may move providers.
