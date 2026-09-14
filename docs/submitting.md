# Submitting results

Results go to Lumina, the AlmaLinux certification web application. Online and
offline submissions are processed identically on the server.

## Choosing a server

Three ways, highest precedence first:

```bash
alma-certify --server https://lumina.example.org <command>   # flag
export ALMA_CERTIFY_SERVER=https://lumina.example.org        # environment
# or [general] server = ... in /etc/alma-certify/alma-certify.conf
```

`--server`, `--run-dir`, and `--config` are accepted either before or after
the subcommand, so both of these work:

```bash
alma-certify --server https://lumina.example.org submit 4f6c2a4e
alma-certify submit 4f6c2a4e --server https://lumina.example.org
```

The environment variable is the convenient one when pointing a machine at a
staging or development instance for a while.

### A server with a self-signed certificate

A dev or staging catalog usually serves one, and by default the suite will not
talk to it at all. `--allow-self-signed` stops verifying the certificate:

```bash
alma-certify --server https://lumina.dev.example --allow-self-signed run
alma-certify run --server https://lumina.dev.example --allow-self-signed
```

Also `[general] allow_self_signed = true` in the configuration file, for a
machine that always talks to that catalog. Every run then prints a line on
standard error saying verification is off, because this is the kind of setting
that gets turned on for an afternoon and lives in a config file for a year.

Read the name as what it does rather than what it says: verification off accepts
**any** certificate from whatever answers on that address, and stops checking
that the name matches it. Use it against a server you know. Where you have a
copy of the certificate, adding it to the host's trust store is the better
answer and leaves this alone.

## Automatic upload

`collect`, `validate`, `benchmark`, and `run` upload their results when they
finish. Authorization is checked before the tests start (and re-checked when
the stored token has under two hours left) so a long run cannot complete only
to find its token expired. Pass `--no-submit` to keep results local.

A failed upload never loses anything: the run stays in the run directory and
`alma-certify submit <run-id>` retries it.

## Token handling

Device-flow tokens are 64 characters, valid for 12 hours, scoped to
submission only, and **bound to the hostname** that requested them. Lumina
refuses results whose report carries a different hostname, so a token copied
off the machine cannot be used to post from somewhere else without also
forging the hostname inside the report. That is not airtight - the client is
open source - but it raises the effort well past reusing a leaked string, and
the operator sees exactly which machine they are authorizing on the approval
page.

Admin-issued tokens (created at `/my/tokens/`) carry no host binding, so they
keep working for scripted uploads from anywhere.

## Online: authorize once, submit many

```bash
sudo alma-certify register
```

That uses `https://catalog.almalinux.org`, the catalog this suite submits to
unless told otherwise. `--dev` points the same command at the staging catalog
at `https://lumina.almalinux.dev`, and `--server URL` at anything else.

The suite prints a URL and a short code. Open the URL in any browser (your
laptop is fine - the machine under test is often headless), sign in to Lumina,
and enter the code. The suite polls until you approve, then stores a
short-lived submit-scoped token at `/var/cache/alma-certify/token.json`, mode 0600
in a 0700 directory, so only root can read it.

No password or long-lived credential is ever typed on the system under test.
If you prefer, `--token` accepts a token issued from `/my/tokens/` directly.

```bash
sudo alma-certify submit <run-id>
```

## Offline

```bash
sudo alma-certify bundle <run-id> -o results.tar.zst
```

Copy the file off the machine and upload it at `/results/upload/` in Lumina.

## Pre-release hardware and embargoes

```bash
sudo alma-certify submit <run-id> --pre-release --publish-after 2026-09-01
```

`--pre-release` marks the hardware as unreleased. With `--publish-after`, an
approved submission stays **completely invisible** to the public - absent from
leaderboards, statistics, feeds, and the read API - until that date. Only you
and Lumina reviewers can see it before then. There is no placeholder that
would reveal the existence of unreleased hardware.

## What the review queues are

- **Validation runs** are reviewed as certification evidence. Approving a run
  that is linked to a system listing contributes to that listing's
  certification status.
- **Benchmark runs** go to a separate queue. They feed the public
  leaderboards and are reviewed for plausibility, not certification.
- **Collect runs** contribute to aggregate hardware statistics.

## Result integrity - what is and is not guaranteed

alma-certify is open-source Python running as root on hardware the submitter
controls. Anyone determined to publish fabricated numbers can edit a report
before submitting it. Pretending otherwise would be dishonest, so the design
does not try to.

What the format does provide:

1. **sha256 manifest** - every artifact in the bundle is hashed, so a report
   cannot silently disagree with its own logs, and transport corruption is
   caught.
2. **Report self-hash** - the report is hashed in canonical form, so partial
   edits are detectable.
3. **Provenance** - suite version, git commit, installed package versions,
   kernel, tuned profile, governor, SMT, and mitigation state are all recorded,
   so a reviewer can tell whether two results are comparable.
4. **Authenticated submission** - every result is bound to a Lumina account
   via a scoped token, which is what actually keeps anonymous garbage out of
   the ingest endpoint.

Deliberately **not** implemented: a per-run HMAC key. With an open client the
key would be handed to exactly the party who might forge results, so it adds
server complexity and no real assurance beyond the bearer token.

Real assurance comes from the server side: plausibility checks against the
declared hardware, artifact logs that must corroborate the reported metrics,
and human review before anything is published.
