# Security

ds-router runs on the machine it is installed on, reads your Hermes config, and for a
gated dashboard it stores a credential you write yourself. It writes nothing outside your
own user directories, and its network requests go to provider usage APIs, model catalogs, explicit health
probes, and the dashboard you configure. Authenticated provider requests refuse
redirects and ignore environment proxy settings. HTTPS verifies certificates.
Plain HTTP dashboards send credentials without TLS, as documented in the README.

## Reporting a problem

Please use GitHub's private reporting rather than a public issue:

https://github.com/btsouth/ds-router/security/advisories/new

That keeps the details out of the issue tracker until there is a fix. If you would rather
not use that form, open an issue saying only that you have a security report and ask for a
contact address. Do not put the details in the issue.

## What counts as a vulnerability here

- A credential being read by ds-router and reaching somewhere it should not: a log line,
  an error message, the process table, shell history, or a URL.
- A path that lets a file you do not control change what ds-router writes or executes.
- A dashboard credential being sent to an origin other than the one it was written for.
- TLS verification being skipped or downgraded without the operator asking for it.

## What is out of scope

- Anything that needs local root, or the ability to edit the files in your own home
  directory. ds-router trusts your `config.yaml` and the credential file you write, the
  same way your shell trusts your `.profile`.
- The security of the providers themselves, or of Hermes.
