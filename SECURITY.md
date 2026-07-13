# Security policy

## Supported versions

Security fixes are released for the latest minor version.

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅         |
| < 0.1   | ❌         |

## Reporting a vulnerability

**Do not open a public issue.**

File a private report through GitHub: the
[Security → Report a vulnerability](https://github.com/happykust/sentry-jaga/security/advisories/new)
tab of the repository. If GitHub is unavailable, email `lhappykust@gmail.com`.

Useful things to include:

- the versions of `sentry-jaga`, Sentry and Python;
- a description of the vulnerability and what it leads to;
- steps to reproduce, or a proof of concept.

## Timelines

- First response — within **72 hours**.
- Assessment and a fix plan — within **7 days** of confirmation.
- The fix and the advisory — by agreement, usually together with the next release; we will
  keep you posted along the way.

Please do not disclose the details publicly until a fix has been released.

## How the integration handles secrets

- The Jaga service account credentials (URL, email, password) are stored in Sentry's
  encrypted `Integration.metadata` field — the same as for every other Sentry integration.
  The package sets up no storage of its own.
- The access token is cached in Sentry's Django cache and renewed when it expires.
- The password and the tokens are never written to the logs and never returned to the UI.

Everything that protects this data at rest is the configuration of your self-hosted Sentry
(`SENTRY_OPTIONS["system.secret-key"]`, access to the database and to the cache). Create a
dedicated service account for the integration, with the minimum rights it needs.

## Outbound requests bypass Sentry's SSRF protection

The Jaga client uses a plain `requests.Session` rather than Sentry's `ApiClient`. That is a
deliberate price paid for the isolation of the core: all of the package's logic lives
without importing `sentry` and is therefore covered by tests that do not need its test
stack. The consequences:

- **Sentry's outbound block list** (`SENTRY_DISALLOWED_IPS`, its SSRF protection) **is not
  applied to these requests**;
- the integration calls exactly the address the administrator typed into the installation
  form — including addresses on the internal network, `localhost` and link-local ones
  (`169.254.169.254` and other cloud metadata services);
- that address lands in `Integration.metadata` and is reused by every subsequent request the
  integration makes.

What this means in practice: installing an integration is a privileged action. The right to
install integrations in an organization effectively grants the ability to make Sentry send a
POST request to an arbitrary address (with a service account login as the body). Therefore:

- **only ever point it at the trusted address of your Jaga instance**;
- keep the right to install integrations with the organization's administrators;
- if your perimeter requires strict control of outbound traffic, enforce it at the network
  level (an egress policy for the Sentry workers) instead of relying on filtering inside the
  package.

We consider this a known and accepted limitation rather than a vulnerability; there is no
need to report it separately.
