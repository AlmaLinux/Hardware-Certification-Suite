# alma-certify - AlmaLinux Hardware Certification Suite

`alma-certify` runs certification workloads on an AlmaLinux system and produces a
versioned, machine-readable report. It replaces the previous Ansible-based
prototype: it runs directly on the machine under test, has real pass/fail
semantics, and emits a JSON report that the AlmaLinux certification web
application (Lumina) can ingest automatically.

There are three kinds of run:

| Run type | What it does |
|---|---|
| `collect` | Normalized hardware inventory - CPUs, DIMMs, disks, NICs, GPUs, firmware, and **driver versions** |
| `validate` | Functional certification tests: does AlmaLinux install and work correctly on this hardware |
| `benchmark` | Performance measurements feeding the public leaderboards |

Validation answers *does it work*, not *how fast* or *how long it survives
load* - workload checks are short functional smoke runs. Anything
performance-shaped belongs in `benchmark`.

## Requirements

- AlmaLinux 8, 9, or 10 on **x86_64 or aarch64**. ppc64le and s390x are not
  supported: `dmidecode` is not built for either, and the suite reads SMBIOS to
  identify the machine it is certifying
- root on the machine under test. Every command needs it; run one without and the
  suite offers to re-run itself under `sudo`
- Python 3.9+ - on AlmaLinux 9/10 that is the system `python3`; on
  AlmaLinux 8 install the AppStream interpreter: `dnf install python3.12`
- `zstd` (AlmaLinux BaseOS) - result bundles are `.tar.zst`
- Repository access, so the suite can install the tools individual tests need
  (`stress-ng`, `fio`, `iperf3`, …). Network access to Lumina is optional -
  see [Offline use](#offline-use).

### Running on something other than AlmaLinux

The suite will run anywhere, because refusing to execute outside the supported
distribution makes it useless for finding out whether the distribution is what
broke. What it will not do is let those results be submitted.

On a non-AlmaLinux host, `collect`, `validate`, `benchmark`, and `run` print a
warning naming the distribution, then ask whether to continue. **Enter declines**
- carrying on is the surprising choice, so it is the one you type. With no
terminal attached (a CI job, a kickstart `%post`) the command refuses instead of
prompting, and `--allow-unsupported-os` is how you say yes in advance.

Such a run is **offline only**:

- no submission token is requested,
- results are never uploaded, and
- `alma-certify submit` refuses it, printing the distribution it ran on.

`alma-certify bundle` still works, so you keep a shareable archive. The refusal is
based on the **report**, not the machine you type the command on, so bundling on
the host under test and uploading from your workstation works normally - and
moving an unsupported run to an AlmaLinux box does not get it past the check.

Lumina applies the same rule at its end: a bundle whose `environment.os.id` is
not `almalinux` is quarantined for a reviewer and can never certify an AlmaLinux
release.

### Running inside a virtual machine

Certifying a machine needs bare metal: inside a guest, the firmware, storage
controller, and network device under test are all the hypervisor's. The suite
still runs there and keeps the results, but a whole-machine run made inside a
virtual machine or a container is **not uploaded**, and `alma-certify submit`
refuses it the same way, judging by the report's recorded
`environment.virtualization`. `alma-certify bundle` still works.

A GPU passed through to a guest is the real device, so `--scope gpu` is the one
claim a guest can make, and such a run submits normally.

### What the suite installs

Most benchmark tools live in EPEL, and many EPEL packages depend on
CodeReady Builder, so before the first install `validate`, `benchmark`, and
`run` will:

1. enable CodeReady Builder (`crb`, or `powertools` on AlmaLinux 8), and
2. install `epel-release`.

Per-test tools are then installed on demand and **left installed** unless you
pass `--cleanup-packages`. Pass `--no-epel` to skip repository setup entirely;
tests whose tools are only in EPEL then skip with a reason instead of running.
Which repositories were active is recorded in the report
(`environment.enabled_repos`), because it determines which tool versions the
results came from.

`collect` never installs anything.

The runtime itself is **stdlib-only**: no pip packages, no virtualenv.

## Install

From RPM (preferred):

```bash
dnf install ./alma-certify-0.1.0-1.el9.noarch.rpm
alma-certify --help
```

From a git checkout (no install step needed):

```bash
git clone https://github.com/AlmaLinux/alma-certify.git
cd alma-certify
sudo python3 -m alma_certify --help          # python3.12 on AlmaLinux 8
```

### Building the RPM

The package is `noarch` and needs no compiler, so one SRPM serves every
release and architecture.

```bash
make rpm                              # for the machine you are on
make mock-rpm DIST=alma+epel-9-x86_64 # clean-room build for one target
make mock-all                         # AlmaLinux 8, 9, and 10
```

Everything lands under `build/rpm/RPMS/` - `make rpm` in `noarch/`, the mock
targets in a directory per target - and each recipe prints the package it
produced. Copy the `.noarch.rpm` to the test systems and
`dnf install ./alma-certify-*.rpm`.

```
build/rpm/RPMS/alma+epel-8-x86_64/alma-certify-0.1.0-1.el8.noarch.rpm
build/rpm/RPMS/alma+epel-9-x86_64/alma-certify-0.1.0-1.el9.noarch.rpm
build/rpm/RPMS/alma+epel-10-x86_64/alma-certify-0.1.0-1.el10.noarch.rpm
```

### Building in Copr

For the builds that are not a release - a branch somebody needs on real
hardware today, an el8 fix nobody wants to tag for - `make copr` submits the
same SRPM to a Copr project:

```bash
make copr COPR_PROJECT=yourname/alma-certify
make copr COPR_CHROOTS="almalinux-9-x86_64" COPR_ARGS=--nowait
```

There is no default project, because a default is where a build quietly goes on
being published long after that is the right place for it. Run at a terminal
with `COPR_PROJECT` unset, `make copr` names the package it built and asks
where to send it; with no terminal, it says what is missing and submits
nothing. `export COPR_PROJECT=...` keeps the answer for a session.

It needs `copr-cli` (`dnf install copr-cli`) and the API token block from
<https://copr.fedorainfracloud.org/api/> saved as `~/.config/copr`, or a path
to one in `COPR_CONFIG`. Which chroots a project builds for is set on the
project itself, so `COPR_CHROOTS` only narrows a single build.

Like every target here, this packages the **working tree**, not `HEAD`, which
is usually the point of a Copr build. The name-version-release is printed
before the upload so what landed in the repository is answerable afterwards.

Full mock logs sit beside each package (`build.log`, `root.log`).

Building with mock is worth the extra minute because the el8 package differs:
AlmaLinux 8's platform python is 3.6, so that build requires the
`python3.12` AppStream interpreter and its launcher execs `python3.12`,
while el9/el10 use the system `python3`. Only a real target root proves that.

The tarball is built from the working tree rather than `git archive HEAD`,
so uncommitted changes are packaged - convenient while developing, and
identical to a tag build in CI where the checkout *is* the tag.

## Quick start

Run `alma-certify` with no arguments at a terminal and it walks you through the common tasks:

```
+-----------------------------------------------------------------------------+
| alma-certify 0.1.0                                              AlmaLinux 10.1 |
+-----------------------------------------------------------------------------+
|                                                                             |
| What would you like to do?                                                  |
|                                                                             |
| > Validate this machine      certification checks                           |
|   Benchmark this machine     performance figures                            |
|   Both, in one run           collect, validate, and benchmark               |
|   Previous runs              look at, resume, or upload a run               |
|   Quit                                                                      |
|                                                                             |
+-----------------------------------------------------------------------------+
  Up/Down move   Enter select   q quit
```

It shows the command your choices add up to, and then runs exactly that:

```
|   $ alma-certify run --scope gpu                                               |
|                                                                             |
|   Start this run                                                            |
```

So the guided mode teaches the CLI: run it once and you have the command for CI. It ships in the
`alma-certify-tui` subpackage with its interface library bundled, so it needs nothing beyond that
one package, and **it only appears when there is a terminal on both stdin and stdout**. In a script, a CI job, or a kickstart `%post`, a bare `alma-certify` prints
the help and exits 3 exactly as it always has. `alma-certify tui` asks for it explicitly.

Everything below works the same whether or not you use it.

```bash
# What would run on this machine?
alma-certify list

# Inventory only
sudo alma-certify collect

# Certification validation
sudo alma-certify validate

# Benchmarks (all categories, or pick some)
sudo alma-certify benchmark --category cpu,memory,crypto

# Everything in one run
sudo alma-certify run
```

Each run prints a summary and a run id. To see the runs already on a machine, newest first:

```bash
sudo alma-certify runs
```

```
RUN       STARTED           TYPES                      CLAIM     SUBMITTED   RESULT
0b71d4c9  2026-08-17 08:14  benchmark                  gpu       no          unfinished, 2 of 14 tests (1 pass, 1 error)
9f3c1a2e  2026-08-16 18:22  collect,validate,benchmark machine   2026-08-16  38 pass, 1 fail, 3 skip
```

`SUBMITTED` is the date the results first reached the server, recorded when an upload succeeds -
whether that was the automatic one at the end of the run or a later `alma-certify submit`. It is the one
thing about a run that cannot be worked out by re-reading it.

The short id in the first column is all the other commands need, so it can be pasted straight into
any of them:

```bash
sudo alma-certify report 9f3c1a2e         # what happened in that run
sudo alma-certify resume 0b71d4c9         # long runs are resumable
sudo alma-certify bundle 9f3c1a2e         # archive it for manual upload
sudo alma-certify submit 9f3c1a2e         # upload it
```

`alma-certify runs --json` prints the same listing for scripts, with the full ids, exact timestamps,
hostname, per-status counts, and the submission's URL. `alma-certify report <id>` names it too,
including whether an uploaded run is still waiting for you to submit it for review.

Exit codes: `0` all passed, `1` one or more failures, `2` errors, `3` usage,
`4` blocked before starting because the host is not AlmaLinux (see
[Running on something other than AlmaLinux](#running-on-something-other-than-almalinux)).

## Submitting results

Runs upload themselves when they finish, to `https://catalog.almalinux.org`
unless you say otherwise:

```bash
sudo alma-certify validate             # runs, then uploads
sudo alma-certify validate --dev       # ... to the staging catalog instead
```

The first run asks you to authorize the machine. You approve it from a
browser, so no password or token is ever typed on the system under test:

```
To authorize this machine (sut-42.lab), open:
    https://catalog.almalinux.org/my/activate/
and enter the code:  BQXK-PMTH
```

That check happens **before** the tests start, and again whenever the stored
token has under two hours left, so a long benchmark pass can never finish and
then discover it has nowhere to send results. Tokens last 12 hours and are
tied to the machine that requested them: results posted from a different host
are refused.

`alma-certify register` still exists if you would rather authorize up front, and
`alma-certify submit <run-id>` uploads any finished run by hand - useful to retry
after a network problem, since a failed upload never discards the run.

### Uploading is not the last step

A validation run arrives as a **draft**. It is stored, but no reviewer sees it
until you add the details the suite cannot detect from the machine: the marketing
name, a description, and a link to the spec sheet. The run says so when it
finishes:

```
== summary ==
  pass  12
  skip  3
test verdict: PASS
  (the required tests passed; certification still needs review)

run id: 4f6c2a4e-9c1b-4d2a-8f77-3a1e5b6c7d80

submitting results to https://catalog.almalinux.org ...

uploaded: https://catalog.almalinux.org/results/runs/4f6c2a4e-.../
======================================================================
 ACTION REQUIRED - this run is NOT submitted for review yet
======================================================================
A PASS verdict means the tests passed on this machine. It does not
mean the hardware is certified: that takes a reviewer, and a reviewer
cannot start until the listing details only you can supply are filled
in - the marketing name, a description, and a link to the spec sheet.

  1. open  https://catalog.almalinux.org/results/runs/4f6c2a4e-.../
  2. add the listing details
  3. press "Submit for review"

Lost the link? It is also on your dashboard, so you never have to keep
this terminal output:

  https://catalog.almalinux.org/my/
  under "My validation runs", listed as "Awaiting submitter details"
  with "Finish submission" in the Action column.

Nothing is reviewed, certified, or published until you do. Until then
this run is visible only to you.
======================================================================
```

**`PASS` is a test result, not a certification.** It means every required test
passed on this machine. Certification is a decision a reviewer makes afterwards.

`benchmark` and `collect` runs need none of this - they have no listing details to
supply, so they go straight to the queue and say `Queued for review. Nothing
further is needed from you.`

To keep results local, opt out:

```bash
sudo alma-certify validate --no-submit
```

Embargoes work the same either way:

```bash
sudo -E alma-certify validate --pre-release --publish-after 2026-09-01
```

`--pre-release` marks unreleased hardware; combined with `--publish-after`,
Lumina keeps the submission invisible to the public until that date (and only
then if a reviewer has approved it).

### Offline use

If the machine cannot reach Lumina at all, skip the upload and hand-carry a
bundle:

```bash
sudo alma-certify validate --no-submit
sudo alma-certify bundle 4f6c2a4e -o ~/alma-certify-results.tar.zst
```

Upload that file at `https://catalog.almalinux.org/results/upload/`. Both paths
go through identical processing on the server.

## Peer-dependent tests

Network throughput checks need a second machine running an iperf3 server:

```bash
# on the peer
iperf3 -s

# on the system under test
sudo alma-certify validate --peer 192.0.2.10
```

Without `--peer` those tests skip cleanly - a skip is not a failure.

## Interactive tests

Some checks need a person at the machine. They never run by default:

- `--interactive` - USB hotplug, suspend/resume, reboot survival

The reboot-survival test installs a one-shot systemd unit so the run resumes
automatically after the machine comes back.

## Configuration

`/etc/alma-certify/alma-certify.conf` (INI) holds defaults for run directory,
server URL, smoke durations, benchmark targets, and per-test timeouts.
See the shipped file for the full list.

Precedence is defaults < config file < environment < command-line flags. The
Lumina URL can come from any of the three, which is handy for pointing a
machine at a dev or staging instance:

```bash
alma-certify --server http://lumina-dev.example:8100 submit 4f6c2a4e
export ALMA_CERTIFY_SERVER=http://lumina-dev.example:8100
```

`--server`, `--run-dir`, and `--config` may be given either before or after
the subcommand.

## Output

A run directory (`/var/lib/alma-certify/runs/<run-id>/`) contains:

```
report.json     the machine-readable report - the contract with Lumina
state.json      resume journal
inventory/      raw tool output (dmidecode, lspci, ethtool, …)
artifacts/      per-test raw logs
alma-certify.log   suite log
```

`report.json` is documented in [docs/schema.md](docs/schema.md), and the test
catalog in [docs/catalog.md](docs/catalog.md).

## Troubleshooting with `--debug`

`collect`, `validate`, `benchmark`, `run`, and `resume` accept `--debug`, which
traces **every command the suite runs** as it happens: the command line, its raw
stdout and stderr, its exit code, and how long it took.

```
[validate.cpu.functional] $ stress-ng --cpu 0 --cpu-method all --verify --metrics -t 60s (timeout 900s)
  --- stdout ---
  stress-ng: info:  [12345] dispatching hogs: 8 cpu
  --- exit 0 in 60.41s ---
[validate.storage.io-sanity] $ dd if=/dev/zero of=/var/tmp/alma-certify.io bs=1M count=64 (timeout 150s)
  --- stderr ---
  64+0 records in
  64+0 records out
  67108864 bytes (67 MB, 64 MiB) copied, 0.0183731 s, 3.7 GB/s
  --- exit 0 in 0.02s ---
```

(That second entry is real output, and shows why the streams are kept apart:
`dd` writes its whole summary to stderr, so a trace that merged them would make it
look like an error.)

Details worth knowing:

- **Each command is labeled with the test that ran it**, so the trace reads as a
  sequence of tests rather than a flat wall of commands.
- **The command is printed before it runs**, so if something hangs you can see
  what it is waiting on. The timeout is shown up front too, so a long wait is
  distinguishable from a stuck one.
- **A missing tool is traced** (`-> command not found`), which is the reason
  behind most skips.
- Coverage is the whole run, not just the tests: inventory collection and package
  installs shell out as well, and both are traced.
- **Output goes to stderr**, so `alma-certify report --json` stays pipeable and
  `alma-certify validate --debug 2> trace.log` saves the trace while the run's own
  summary still scrolls past. Nothing is added to `alma-certify.log`, and nothing
  changes in `report.json` or the bundle.
- `--debug` output is **not redacted**. `--redact` strips serials and UUIDs from
  `report.json`; the trace is raw tool output, so serials can appear in it. Using
  both prints a reminder. Check a trace before pasting it into a bug report.

Per-test raw logs are also written to `artifacts/` on every run, with or without
`--debug`; the flag is for watching it live and for the commands whose output is
not kept as an artifact.

## Result integrity

The suite is open-source Python running on hardware the submitter controls, so
nothing in a bundle can cryptographically prove results are genuine. What the
format does provide: a sha256 manifest of every artifact, a self-hash of the
report, the exact suite version and commit, and submission bound to an
authenticated Lumina account. Everything beyond that is server-side
plausibility checking and human review. See
[docs/submitting.md](docs/submitting.md).

## Development

```bash
pip install pytest ruff
python -m pytest tests -ra
ruff check alma_certify tests
```

The language floor is Python 3.9 and the runtime must stay stdlib-only -
`pytest` and `ruff` are development-only dependencies. Adding a test means
adding a `Test` subclass and registering it; see
[docs/catalog.md](docs/catalog.md#adding-a-test).
