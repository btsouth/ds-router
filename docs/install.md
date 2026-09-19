# Installing, verifying and undoing ds-router

The README's quick start is the short version. This is the long one: what the
installer assumes, how to tell whether it worked, what each failure message means,
how to undo it, and what every key in `config.yaml` does.

Nothing here is invented for the doc; every command is one the scripts actually
implement, and every failure message quoted is one the code actually prints.

## Prerequisites

| what | why | how |
|---|---|---|
| the `hermes` CLI on PATH | ds-router steers Hermes; without it there is nothing to steer | <https://hermes-agent.nousresearch.com/install.sh>; its launcher usually lands in `~/.local/bin`, which is often not on a non-login shell's PATH |
| Python 3.10+ with `pyyaml` | the router is standard library plus PyYAML | `sudo apt install python3 python3-yaml`, `sudo dnf install python3 python3-pyyaml`, `sudo pacman -S python python-yaml`, or `python3 -m pip install --user pyyaml` |
| `openssl` | `install.sh` runs the whole test suite as a preflight, and the TLS checks generate a throwaway certificate per run | `sudo apt install openssl`, `brew install openssl` |

`openssl` is the one that surprises people: it is not needed at runtime, only to
install, because the installer refuses to install a tree whose own tests do not pass.

## Install

```sh
git clone https://github.com/btsouth/ds-router ~/Projects/ds-router
cd ~/Projects/ds-router
./install.sh --dry-run    # print every action, change nothing
./ds-switch --show        # preview before enabling the timer
./install.sh              # install and enable the Linux timer
```

The checkout can live outside `~/Projects/ds-router`: the installer rewrites the
units and manifest for its actual location.

Installation files:

- `~/.config/systemd/user/ds-router.{service,timer}`, enabled (Linux), or
  `~/Library/LaunchAgents/com.ds-router.switch.plist`, **not loaded** (macOS; the
  commands to load it are printed)
- an optional `~/.local/bin/ds-switch` symlink (skip with `--no-symlink`)
- a manifest at `$XDG_STATE_HOME/ds-router/install-manifest`, which `uninstall.sh`
  reads to know what to reverse

It never uses `sudo`. Preflight checks use private temporary storage that is
removed on exit. The Linux timer is enabled immediately and can run `ds-switch`,
which writes Hermes configuration. Install with `--no-service` for a manual-only
setup. Re-running the installer preserves identical installed files; it also
ensures the timer is enabled.

Relevant flags: `--dry-run`, `--no-service`, `--no-symlink`, `--yes`,
`--skip-preflight`. `./install.sh --help` lists them.

## Verifying it worked

```sh
./ds-switch --check                              # does the config drive Hermes?
./ds-switch --show                               # what would it pick, writing nothing
./ds-switch                                      # apply it
systemctl --user list-timers ds-router.timer     # is the timer armed, and when next
journalctl --user -u ds-router.service           # what the last tick decided, and why
```

`ds-switch --check` ending in `config OK` means the config is consistent. If it says
`config OK, but no provider key is set in this shell or in ~/.hermes/.env`, the
config is fine but nothing can be fetched yet; add a key (README, "Before you
start").

**One provider can set the default, but provides no failover alternative.** An
already matching configuration is not rewritten.

These preview commands never rewrite Hermes configuration. Automatic
`ds-switch --show` reads through the `hermes` CLI, which can bootstrap `~/.hermes`
(a skills directory, a log, an empty session store)
the first time it runs.

## Undoing it

Three levels, from least to most:

```sh
./ds-switch --off         # set default_provider once; the timer is still enabled
./uninstall.sh --dry-run  # see what would be removed
./uninstall.sh            # stop and remove the timer, the symlink and the manifest
```

Neither of those deletes your Hermes config. To put it back exactly as it was before
ds-router ever touched it, restore the backup it made:

```sh
cp ~/.hermes/config.yaml.bak-ds-router ~/.hermes/config.yaml
```

That backup is written once, the first time `ds-switch` rewrites the config, and is
never overwritten afterwards; so it holds the before-picture, not the previous tick.
If modified units are found, uninstall stops/disables them but preserves their
files and manifest, reports an incomplete uninstall, and exits 1. Inspect them
and retry with `--force` to remove them. Do not assume an incomplete uninstall
stopped routing, especially if service-manager commands failed.

The uninstaller leaves the checkout, `~/.hermes`, PyYAML and quota snapshots alone.

## Troubleshooting

| symptom | cause | fix |
|---|---|---|
| `python3 is not on PATH` | no Python 3 | install Python 3.10+ (the message lists distro commands) |
| `the 'hermes' CLI is not on PATH` | Hermes missing, or its launcher is not on a non-login shell's PATH | install Hermes; add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile |
| `pyyaml is not importable` | PyYAML missing | the installer offers to `pip install --user` it; otherwise install your distro's `python3-yaml` |
| the installer stops with `test suite FAILED: refusing to install a broken tree` | the preflight suite failed; most often `openssl` is missing, so the TLS checks cannot run | install `openssl` and re-run; or run `python3 run_tests.py` yourself to see the real failure, then `./install.sh --skip-preflight` |
| `no reachable systemd --user session` | you are not in a login session the user manager can reach (common over `ssh` or `su`) | the units were still written; run `systemctl --user daemon-reload && systemctl --user enable --now ds-router.timer`, and `loginctl enable-linger "$USER"` if it must survive logout |
| the timer is `active` but Hermes never moves | either every provider is healthy (nothing to do), a quota endpoint is unreachable (a failed *reading* is deliberately not a reason to move), or there is no alternative provider | `./ds-switch --show` prints the decision and its reason; `journalctl --user -u ds-router.service` prints the last tick |
| `ds-switch --check` exits 1 | the config has a real problem (missing `base_url`/`key_env`, unknown `default_provider`, `default_model` not matching the alias) | the problems are listed above the summary line |
| `router.py` or `placement.py` exits 1 with `config: ...` | `config.yaml` (or the `--config` file) is unreadable, malformed, or not a mapping | fix the file; the message names the key |
| `refusing to route: unreadable concurrency cap(s)` | a `caps:` entry is not a positive integer (`caps: 3`, `{limit: 3}`, `"three"`) | make it a number, or remove the entry; a cap that cannot be read is not "unlimited" |
| `placement.py` exits 3 | no reachable Hermes backend | start the backend, or read the README's section on a gated dashboard |
| commands not found after install | `~/.local/bin` is not on PATH | use `~/Projects/ds-router/ds-switch`, or add `~/.local/bin` to PATH |

Exit codes for scripting are in the README ("Exit codes").

## Configuration reference

`config.yaml` is the single source of truth: `router.py`, `apply.py` and
`placement.py` all read it, and nothing about a provider lives in the shell wrapper.
The full schema is the comments in the file itself; this is the map.

| key | meaning |
|---|---|
| `default_model` | the logical alias the router routes (e.g. `deepseek-v4.1-flash`) |
| `default_provider` | what `--off` hands Hermes back to, and what `--check` expects |
| `models:` | alias → provider → that provider's own model id. Adding a model is one entry here and no code |
| `providers:` | per provider: `base_url`, `key_env`, `quota:` (the reader kind), optional `session_header` |
| `routing.skip_at` | a provider is "spent" once its tightest window reaches this fraction |
| `routing.window_weights` | how much each window class counts toward risk (session/weekly/monthly) |
| `routing.concurrency.enabled` | whether the in-flight cap is applied at all |
| `routing.concurrency.caps` | provider → max concurrent requests; zero or null means no declared cap. Per *account*, not per host; declare less on a second machine sharing one account |
| `routing.concurrency.pressure_per_over` | risk added per session over a cap (kept well below a real exhaustion risk) |
| `routing.concurrency.max_pressure` | ceiling on that contribution, so an overloaded provider stays reachable |
| `routing.quota_ttl_seconds` | snapshots older than this are treated as unknown |
| `routing.reuse_collector_state` / `collector_state_dir` | optionally reuse one JSON-per-provider cache instead of polling. Absent directory → poll directly; optional and safe to leave off |
| `peak.preferred_tiebreak` / `peak.windows` | time-of-day pricing as a **tie-break only**; malformed spans are ignored with a note rather than silently meaning "never at peak" |
| `gateway:` | optional: steer a dashboard-gated backend by signing in as a browser does. Omit it and nothing changes |

Deliberately **not** present, so its absence does not look like an oversight: there is
no `listen:`/port key and no proxy configuration, because ds-router is not an HTTP
server and does not sit in the request path. It steers Hermes' provider choice; it
never forwards a request. Mid-turn failover is Hermes' own `fallback_providers` chain,
not this tool.
