# Contributing

Small project, one maintainer, most of it in two files. Two things make a patch land
quickly.

## Run the gate

```sh
python3 run_tests.py                                          # every suite
for f in install.sh uninstall.sh ds-switch; do sh -n "$f"; done
shellcheck -s sh install.sh uninstall.sh ds-switch
```

The suite has to pass with no Hermes install, no provider keys and no network: CI runs it
on a bare runner, so a test that reads your own `~/.hermes` passes for you and fails
there. Most suites run through `testkit.run` so that a failure reports its own name.
`tests.yml` shows the matrix: ubuntu and macOS, python 3.10 and 3.12.

`./install.sh --help` must return as well. The usage text is printed by stripping `#`
from the header comment block, so a line in that block without a `#` becomes a command
the shell executes on startup. That happened once: the installer re-ran itself with its
own help text as arguments until the machine refused. `sh -n` and shellcheck both pass a
line like that, which is why `test_install.py` runs `--help` and checks it returns.

## Bring the test that would have caught it

A behaviour change lands with a test that goes red when the change is reverted. Revert
your own fix and check before opening the pull request. A test that survives its revert
is decoration, and this project has shipped a few of those.

## Adding a provider

See "Adding a provider" in the README, and the "New provider" issue template: a usage API
that reports real quota, with a sample response, is the whole requirement. Providers whose
quota cannot be read are already listed in `docs/candidate-providers.md`, with the reason:
check there before proposing one.

## Reports and pull requests

Do not paste credentials: no API keys, no `.env`, no dashboard URL carrying a username or
password. A report is actionable with the output of `ds-switch --check` and
`router.py --dry-run`, which print no keys. Security problems go through
[SECURITY.md](SECURITY.md) rather than the issue tracker.
