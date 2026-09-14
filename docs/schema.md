# alma-certify result schema (v1.1)

This document is the contract between the `alma-certify` suite and the Lumina web
application. Both projects validate against it. Changes require a
`schema_version` bump: additive optional fields bump the minor version,
breaking changes bump the major version.

## The bundle

A completed run is packaged as a zstd-compressed tarball (Lumina also
accepts legacy gzip; the compression is sniffed from magic bytes):

```
alma-certify-<hostname>-<run_id>.tar.zst
├── report.json          # authoritative machine-readable report (this schema)
├── SHA256SUMS           # sha256sum-format digest of every other file
├── inventory/           # raw tool outputs (dmidecode.txt, lshw.json, ...)
└── artifacts/           # per-test raw logs, one directory per test id
    └── <test-id>/...
```

Rules:

- `report.json.artifact_manifest` must list every file in `inventory/` and
  `artifacts/` with its sha256 and size. Lumina rejects the whole bundle on
  any mismatch or on files present in the tarball but absent from the
  manifest (and vice versa).
- All members are plain files under relative paths. No symlinks, devices,
  or absolute/`..` paths.
- `report.json` is UTF-8, and its self-hash (below) is computed over the
  canonical form: `json.dumps(report, sort_keys=True, separators=(",", ":"))`
  with `integrity.report_sha256` set to `null`.

## report.json

Top-level object:

| key | type | description |
|---|---|---|
| `schema_version` | string | `"1.1"` (1.0 lacked `baseboard` and `system.kind`; `system.model_number`, `chassis`, and `bmc` were added later within 1.1 and are absent from older reports) |
| `run` | object | run metadata |
| `environment` | object | OS/tuning state of the machine |
| `inventory` | object | hardware inventory (`summary` + `raw`) |
| `results` | array | per-test results |
| `artifact_manifest` | array | `{path, sha256, size}` for every bundled file |
| `integrity` | object | `{report_sha256}` self-hash |

### `run`

| key | type | notes |
|---|---|---|
| `run_id` | string | UUUIDv4, generated at run start. Lumina idempotency key. |
| `suite_version` | string | semver of alma-certify |
| `suite_git_commit` | string\|null | short hash if known |
| `run_types` | array of string | subset of `["collect", "validate", "benchmark"]` |
| `target_type` | string | `"hardware"` (future: `"cloud_instance"`). What kind of *host* the run happened on. |
| `claim_scope` | array of string | Component kinds this run is a claim **about**. `gpu` is the only kind accepted: it is the only component a virtual machine sees as the real device. Absent or `[]` means the whole machine, which is the default and what every report before this field said. Set by `--scope`. See below. |
| `hostname` | string | |
| `started_at` / `finished_at` | string | ISO 8601 UTC, `Z` suffix |
| `pre_release` | boolean | hardware is unreleased; submission is embargoed |
| `publish_after` | string\|null | `YYYY-MM-DD`; earliest publication date |
| `interactive_included` | boolean | interactive tests were run |
| `resumed` | boolean | run was resumed at least once |
| `sleep_inhibited` | boolean | the machine was stopped from suspending for the run. False where logind could not be asked |
| `cpu_governor_forced` | boolean | performance was forced for the benchmark and restored after: the power-profiles-daemon `performance` profile where one runs, else the raw CPU governor. False for a validate-only run, where it was already on performance, or where nothing could be set |
### `claim_scope`

A run with a non-empty `claim_scope` is evidence about the named components and about nothing else.
It exists so a GPU can be certified on its own, including one passed through to a virtual machine:
the card is the product somebody wants certified, and the guest around it is rented, shared with
other tenants, and different silicon on its next boot.

`gpu` is the only kind the suite emits, and that is a deliberate limit rather than an unfinished
list. A GPU is the one component a guest sees as the real device; a virtualized CPU, NIC, or disk is
the hypervisor's model of one, so certifying those needs bare metal and a whole-machine run. The
server's validator accepts any known component kind, so a future kind needs no ingest change.

This is not the same as narrowing which tests run. `--category gpu` runs only the GPU tests and
still submits a claim about the whole machine, so the server sees a machine validation that skipped
nearly everything. `--scope gpu` narrows the claim, and the server then refuses to certify the host:
no system listing is created, linked, or attested, and no component outside the scope is tied. It
also narrows the tests, unless `--category` is given explicitly.

Two consequences worth knowing before using it:

- The server refuses to certify a claimed kind with no **gating** result in its categories. A
  result that is informational, or that failed, is not evidence, and the rule is about the result's
  category and severity rather than any named test: any required `gpu` check that passed will do,
  whether that is a CUDA check on an NVIDIA card or the OpenCL and Vulkan checks on anything else.
  `validate.gpu.driver` alone is informational on purpose and proves only that a driver was seen,
  and a check that skipped because the hardware offers no such API is not evidence either.
- Values are component kinds, not test categories. The two are spelled alike for `gpu`, and will not
  be for every future kind: a network claim would be scope `nic` against category `network`.


### `environment`

| key | type | notes |
|---|---|---|
| `os` | object | `{id, version_id, kernel, arch}` from os-release/uname |
| `selinux` | string | `enforcing` / `permissive` / `disabled` / `unknown` |
| `secure_boot` | string | `enabled` / `disabled` / `unknown` |
| `kernel_taint` | integer | `/proc/sys/kernel/tainted` |
| `tuned_profile` | string\|null | active tuned profile |
| `power_profile` | string\|null | active power-profiles-daemon profile (power-profiles-daemon on 9, tuned's ppd shim on 10), or null where no such daemon answers. Recorded beside the governor because on an active-mode pstate driver it, not the governor, decides clock |
| `cpu_governor` | string\|null | scaling governor of cpu0 |
| `smt` | boolean\|null | SMT/hyperthreading active |
| `mitigations` | object | `{vuln_name: status_string}` from sysfs |
| `installed_packages` | array | `{name, version, repo}` packages the suite installed |
| `enabled_repos` | array of string | extra repositories active during the run (e.g. `["crb", "epel"]`); part of the comparability context, since they decide which tool versions were available |
| `nvidia_driver` | object | **present only on a machine with an NVIDIA card.** Where the driver in the running kernel came from. See below |
| `virtualization` | object | What was virtualizing the machine, if anything: raw signals and no verdict. See below |

#### `environment.virtualization`

`{type, container, detector, signals}`. `type` is `systemd-detect-virt --vm`'s word (`none` on bare
metal, else `kvm`, `vmware`, `amazon`, ...), `container` its `--container` word or null, and
`detector` which of the two routes answered. `signals` holds the corroborating raw reads: the CPU
hypervisor flag (x86 only, null elsewhere), `/sys/hypervisor/type`, the DMI vendor and product
fields, s390x and ppc64 markers, container markers, and `arch`.

Recorded rather than judged. The suite refuses to *upload* a whole-machine claim made inside a guest
(a `--scope gpu` run is the one claim a guest can make), but the report states only what it saw;
lumina derives its own answer from the same facts so the policy can change server-side.

#### `environment.nvidia_driver`

Added in 1.1 without a version bump, additively: it is absent on every machine with no NVIDIA card,
and a server that predates it ingests a report carrying it unchanged.

A GPU pass from a driver this suite loaded during the run is not the same evidence as one from a
machine that booted with the driver up. Suppressing nouveau lands on the kernel command line and in
the initramfs, and no boot has seen either yet; the driver's own packaging agrees, shipping an
`/etc/dnf/plugins/needs-restarting.d` file that makes `dnf needs-restarting -r` exit 1 for the rest
of that boot. Whether such a pass may certify is Lumina's decision, so this section is facts.

| key | type | notes |
|---|---|---|
| `loaded_by_alma_cert` | boolean | this process ran the modprobe. The one field that cannot be reconstructed later |
| `present_before_run` | boolean | the driver was already in the kernel when the suite started looking |
| `modules_before` | array of string | the `nvidia*` modules in `/proc/modules` before the suite touched anything |
| `conflicting_modules_before` | array of string | present only when one was loaded: the in-tree driver (`nouveau`, `nova_core`) holding the card, which is the usual reason a GPU run certifies nothing after a successful install |
| `modules_after` | array of string | the same list after the attempt |
| `attempts` | array | `{argv, returncode, timed_out, stderr}` per modprobe, stderr verbatim |
| `nvidia_smi` | string | what `nvidia-smi -L` said, verbatim, including its failure text |
| `running_kernel` | string | `uname -r` at the time of the attempt |
| `newer_kernel_installed` | boolean\|null | a newer kernel of the same flavour has modules installed, so the next boot is not this one. `null` if `/lib/modules` could not be read |
| `installed_during_run` | array of string | the driver install commands the run executed. Not visible in `installed_packages`, which only tracks packages installed through `alma_certify.pkg` |
| `install_failed_at` | string | present only if the install stopped part way, naming the command that failed |

### `inventory`

`inventory.summary` is the normalized cross-tool view Lumina consumes.
`inventory.raw` maps source names to bundle paths of untouched tool output.

`summary` keys:

- `system`: `{vendor, product, model_number, version, family, serial, uuid,
  kind, bios: {vendor, version, date}}`
  - `product` is the readable model. Where DMI type 1 Product Name holds only
    an opaque machine-type code (Lenovo reports `21K9001NUS`), the readable
    name is taken from type 1 Version or Family - the field `lshw` shows as
    the system's `version` - and the code is kept in `model_number`. Vendors
    who already put a readable name in Product Name (`PowerEdge R760`) send
    `model_number: null`.
  - `kind` is `prebuilt` (DMI names a vendor system model, such as a Dell
    R720 or a Framework Laptop 13), `custom` (it does not: the table is
    placeholder text or merely mirrors the motherboard, so the board is the
    defining component), or `unknown`. This is about whether a **vendor model
    exists**, not about who assembled the machine: people build servers, and
    Framework ships laptops as kits.
- `baseboard`: `{vendor, product, version, serial}` - the motherboard
  (DMI type 2). On custom builds this, plus the CPU, is the machine's
  real identity
- `chassis`: `{type, vendor}` - form factor from DMI type 3 (`Notebook`,
  `Rack Mount Chassis`, `Desktop`). Recorded for filtering only; it is never
  used to infer how a machine was built
- `bmc`: `{present}` and, when present, `{interface, ipmi_version,
  i2c_address, nv_storage}` from DMI type 38. Lets the IPMI tests decide
  applicability without loading a driver or installing `ipmitool`. **The
  BMC's network configuration is deliberately never collected**: a management
  interface address is a live attack surface and these reports get published
- `cpus`: list of `{model, vendor, sockets, cores, threads, max_mhz, flags, flags_virt}`. `flags` is the CPU's full advertised feature set as a **sorted, de-duplicated list** of lowercase names, from `lscpu`'s `Flags` on x86_64 or `Features` on aarch64; it is `[]` when the system reports none. Sorted so two runs of the same CPU produce byte-identical output whatever order `lscpu` used, which makes two machines diffable. `flags_virt` is derived from it (`vmx`, `svm`, or `""`) and kept because it is the one flag question asked often enough to deserve its own field. The same list is reported as the informational result `validate.cpu.flags`, which additionally groups the notable entries - see [catalog.md](catalog.md).
- `memory`: `{total_bytes, slots_total, slots_populated, dimms: [{locator, bank_locator, size_bytes, type, speed_mts, rated_speed_mts, rank, manufacturer, part_number}]}` from DMI type 17. `speed_mts` is the configured (actual) clock, `rated_speed_mts` what the module is rated for; a platform clocking DDR4-2400 down to 1600 is a fact about the platform and benchmark results are not comparable without it. `bank_locator` and `rank` are here because channel population and rank move memory bandwidth more than capacity does. Sizes are parsed from both dmidecode spellings (`16 GB` and `16 GiB`, which 3.5 switched to); only populated slots appear in `dimms`.
- `disks`: list of `{name, model, serial, bytes, transport, rotational, firmware, driver}`
- `nics` and `gpus` are runtime **views**, not the definition of those categories - see
  `pci_devices` below, which is the authoritative enumeration the server categorizes from. They
  carry the data lspci alone does not (a NIC's live MAC/speed/link, a GPU's `nvidia-smi` name), and
  each joins back to a `pci_devices` entry by its `pci` slot. They are also the fallback for bundles
  written before `pci_devices` existed.
  - `nics`: list of `{name, pci, mac, driver, driver_version, firmware, speed_mbps, link}`. This is
    the set of kernel network interfaces, so a card with no bound netdev is absent here but still
    present in `pci_devices`.
  - `gpus`: list of `{pci, vendor, model, driver, driver_version, runtime, vbios}` -
    `runtime` is an object like `{"cuda": "12.8"}` / `{"rocm": "6.2.1"}` /
    `{"mesa": "24.1"}`.
    AMD integrated graphics additionally carry `asic` and `integrated: true`:
    lspci reports only the die codename for them (`Phoenix1`, `Renoir`), so the
    product name is taken from the CPU brand string it shares a package with
    (`AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics` gives `Radeon 780M`) and the
    codename is preserved in `asic`. Discrete cards are bracketed by pci.ids and
    keep their own name untouched
- `pci_devices`: list of `{pci, class, class_id, pci_ids: {vendor, device, subsystem_vendor,
  subsystem_device}, driver}` - every device `lspci -vmmnnk` enumerated, passed through raw so the
  **server** decides what each is (a NIC is `class_id` `02xx`, a GPU `03xx`) and which to exclude,
  reprocessably. `class` is the human string (`Ethernet controller [0200]`), `class_id` its 4-hex
  code (`0200`). Additive within schema 1.1: a server that predates it ignores it and reads the
  `nics`/`gpus` views above. Empty when `lspci` is unavailable
- `drivers`: `{kernel: "<uname -r>"}` plus any notable controller drivers

Serial numbers and UUIDs may be `null` when `--redact` is used.

### `results[]`

| key | type | notes |
|---|---|---|
| `id` | string | dotted, e.g. `validate.storage.smart`, `bench.cpu.sysbench-multi` |
| `run_type` | string | `collect` / `validate` / `benchmark` |
| `category` | string | e.g. `storage`, `cpu`, `gpu` |
| `severity` | string\|null | validate only: `required` / `conditional` / `informational` |
| `status` | string | `pass` / `fail` / `skip` / `error` |
| `reason` | string\|null | skip reason / failure summary |
| `started_at` | string\|null | ISO 8601 UTC |
| `duration_s` | number\|null | wall time |
| `metrics` | array | see below; empty for non-benchmark tests usually |
| `details` | object | free-form; GPU results MUST include `driver_info` |
| `artifacts` | array of string | bundle paths of this test's raw logs |

Metric object:

| key | type | notes |
|---|---|---|
| `name` | string | e.g. `iops`, `throughput` |
| `value` | number | |
| `unit` | string | e.g. `MB/s`, `events/s`, `ns` |
| `direction` | string | `higher_is_better` / `lower_is_better` / `info` |
| `primary` | boolean (optional) | at most one per result; leaderboard default |
| `device` | string (optional) | present when a benchmark ran on more than one device of the same kind (clpeak on each GPU): the raw device string that produced the figure. The same metric `name` then appears once per device, so Lumina keys GPU results per model rather than collapsing to the fastest card |
| `device_ordinal` | integer (optional) | set only under `--all-gpus`: the 0-based position of this card within its identically named group, so two physically identical cards (which share a `device` string and carry no PCI id) submit as individual results. Absent in the default run, where identical cards are benchmarked once |

Benchmark results additionally carry `details.benchmark_version` (string) -
the version of the benchmark definition, used by Lumina as part of the
leaderboard comparability key `(id, benchmark_version, metric)`.

### Validation verdict

A validate run is *certification-passing* when there are zero `required`
failures/errors and zero `conditional` failures/errors among applicable
(non-skipped) tests. `informational` results never gate. The verdict is
computed, not stored - both sides derive it from `results[]`.

## Integrity model (honest scope)

The suite is open-source Python running on hardware the submitter controls;
nothing in the bundle can cryptographically prove the results are genuine.
What the format provides:

1. sha256 manifest - transport corruption / accidental tamper detection,
2. report self-hash - the report matches the manifest that was hashed,
3. `suite_version` + `suite_git_commit` - reproducibility context,
4. authenticated submission (Bearer token bound to a Lumina account).

Trust beyond that comes from Lumina's server-side plausibility checks and
human review.

## Submission API (Lumina)

- `POST /api/v1/device/code` → `{device_code, user_code, verification_uri,
  verification_uri_complete, expires_in, interval}`. `verification_uri_complete`
  is the RFC 8628 field: `verification_uri` with the code already in it
  (`?code=<user_code>`), so `alma-certify register` renders it as a QR the operator
  scans to reach the approval page with the code filled in - the URL and code are
  still printed as the fallback. The QR is skipped where it would not scan (no
  terminal, color disabled, too small) and by `--no-qr` / `ALMA_CERTIFY_NO_QR`.
- `POST /api/v1/device/token` - RFC 8628-style polling; success returns
  `{token, expires_at, scopes: ["submit"]}`
- `POST /api/v1/results/` - multipart: `bundle` (the tarball), optional
  `pre_release` (`true`/`false`) and `publish_after` (`YYYY-MM-DD`) form
  fields overriding the report values. Auth: `Authorization: Bearer <token>`
  with `submit` scope.
  - `201` `{uuid, status, web_url}` - accepted
  - `200` `{uuid, duplicate: true, web_url}` - exact same bundle already ingested
  - `409` - same `run_id`, different content
  - `400` `{code, detail}` - validation failure (`unsupported_schema`,
    `manifest_mismatch`, `invalid_report`, `bad_archive`)
  - `413` - bundle exceeds the server size cap
