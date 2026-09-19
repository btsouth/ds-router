# v0.1.0 release validation

Validated on Linux on September 19, 2026, using Python 3.14.7, OpenSSL 3.6.4,
and ShellCheck 0.11.0. Tests use fake credentials, local servers, disposable homes,
and fake service managers. No production Hermes sessions were changed.

## Checks

```sh
python3 run_tests.py
for f in install.sh uninstall.sh ds-switch; do
  sh -n "$f"
  "./$f" --help
done
shellcheck -s sh install.sh uninstall.sh ds-switch
```

The suite has 242 tests across 10 suites. The Linux release gate also runs it in
a network namespace containing only loopback, with the rest of the filesystem
read-only and temporary files confined to the checkout. This checks that tests
need neither provider access nor the developer's Hermes state.

Install and uninstall dry-runs were checked with fake prerequisites. The full
installer preflight left no new paths in its scratch parent. An install followed
by another install preserved installed file contents and modification times;
uninstall removed the sandbox installation. These checks do not enable the real
user timer.

The [CI workflow](../.github/workflows/tests.yml) runs the suite on Ubuntu and
macOS with Python 3.10 and 3.12. Release publication requires all four jobs to pass.

## Regression evidence

`test_release.py` covers credential redirects and error reflection, private
temporary files in both scripts, concurrent config writers, missing endpoints,
unchanged configs, backup failures, live-to-database fallback, external load,
invalid configuration, collector/catalog failures, unmetered providers, pricing
ties, and incomplete uninstall results.

Against the reviewed pre-fix source snapshot, 19 of these 20 tests fail. The one
that passes verifies pre-existing installer idempotence. With the fixes, all 20
pass. The changed failed-send test in `test_transport.py` also fails when its
transport fix is reverted: the old implementation reconnects twice instead of
refusing to replay a request whose delivery is unknown.

For future changes, run the relevant regression, revert only the behavior fix
in an isolated copy, and confirm that the regression fails before restoring it.

## Boundaries

- Provider quotas, prices, and historical Hermes observations in the README were
  measured during development. They were not re-polled with production keys for
  this release.
- Live session mutation and the real systemd timer were not re-exercised during
  this hardening pass. The automated checks use isolated substitutes.
- macOS tests cover code and installation behavior; loading and running the
  launchd job on a real Mac remains unverified.
- A routing lock coordinates ds-router writers, not other Hermes configuration
  editors. Abrupt termination between Hermes CLI writes cannot run rollback.
- Placement uses a snapshot. Deferred moves and other clients can still produce
  temporary concurrency pressure; ds-router does not intercept requests.
