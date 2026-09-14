# Test catalog

Run `alma-certify list` for the live catalog on any machine. This document
explains what each test does and why.

## Validation tests

Validation answers **does AlmaLinux work correctly on this hardware**. It is
not stress testing: workload-shaped checks are short functional smoke runs
(seconds to about two minutes) that verify correct behavior, not endurance.
Anything measuring speed belongs in the benchmark catalog.

Severity determines whether a test gates certification:

- **required** - always applicable; a failure fails certification
- **conditional** - depends on hardware or a peer being present; skips
  cleanly with a reason when not applicable, but fails certification if it
  runs and fails
- **informational** - recorded for the reviewer, never gates

`interactive` tests need a person at the machine (`--interactive`).

### CPU and memory

| Test | Severity | What it checks |
|---|---|---|
| `validate.cpu.functional` | required | `stress-ng --cpu-method all --verify` for ~60s. Every CPU method must compute correct results; stress-ng exit codes map to human-readable causes. |
| `validate.cpu.flags` | informational | The CPU's advertised feature flags, read from the collected inventory rather than by running `lscpu` a second time. Reports the full sorted list plus the notable entries grouped by what they tell you: virtualization, confidential computing, crypto acceleration, vector extensions, speculation controls, timing and scheduling. Absent groups are omitted - these are capabilities, and a CPU is not defective for lacking AMX. Never fails and never affects the verdict; skips only when the system reports no flags at all. |
| `validate.memory.functional` | required | Short verified `--vm` pass, plus EDAC/MCE counters must not increase during it. |
| `validate.memory.edac` | conditional | EDAC counters readable; uncorrectable errors fail, correctable errors are reported but do not gate. |

### Storage

| Test | Severity | What it checks |
|---|---|---|
| `validate.storage.smart` | required | Per-disk `smartctl -H` PASSED; NVMe `critical_warning` and `media_errors` must be zero. |
| `validate.storage.io-sanity` | required | Two short `fio` passes against a file in one directory, plus a before/after dmesg I/O error scan. First a sequential write with `--verify=crc32c --do_verify=1`, so every block read back is one this run wrote - data must survive the round trip. Then time-based 4k random read/write with no verify attached, which is what the dmesg delta judges. Verification and mixed random I/O cannot share one job: with `rw=randrw` the read side picks offsets this run has not written and finds no checksum header there, so the check reported a verify failure on a healthy disk. A `fio` exit with no JSON report is an **error**, not a failure - it means the job never ran, and calling that a failed verify accuses a disk of corrupting data that was never tested. The target filesystem is probed first: a RAM-backed one (`tmpfs`, `ramfs`) **skips**, because passing an I/O check run against memory would claim the storage was validated, and one that refuses `O_DIRECT` (overlayfs in a container, older kernels) falls back to buffered I/O with `--end_fsync=1` and records `direct_io: false`, rather than losing the check entirely. Set `storage_target_dir` in the `[benchmark]` section to choose where it writes. |
| `validate.storage.nvme-errorlog` | conditional | NVMe smart-log has no critical warnings or media errors. |

### Network

| Test | Severity | What it checks |
|---|---|---|
| `validate.network.pci-drivers` | required | Every PCI network controller (wired and wireless) has a kernel driver bound and produced an interface. Uses `lspci -k` rather than `ip link`, because a controller with no driver creates no interface and would otherwise be invisible. |
| `validate.network.link` | required | Every physical NIC has a bound driver; at least one has link. Records driver, driver version, firmware, and negotiated speed. |
| `validate.network.datapath` | conditional | Needs `--peer`. Short iperf3 run: traffic flows and reaches a sane fraction (default 50%) of the NIC's link rate. A functionality gate, not a throughput measurement - for numbers, see `bench.net.*`. |

### Kernel and platform

| Test | Severity | What it checks |
|---|---|---|
| `validate.kernel.taint` | required | Fails only on taint bits that mean a fault: MCE (M), bad page (B), OOPS/BUG (D), soft lockup (L). Out-of-spec/unsupported hardware (S) is reported for review with the kernel's own log line, not failed: RHEL-family kernels use that bit for vendor support policy, and such machines commonly work. Firmware workarounds (I), the unsupported-module marker (X), staging drivers (C), proprietary/out-of-tree/unsigned modules (P/O/E), live patching (K), and bare warnings (W) are recorded only. |
| `validate.kernel.dmesg` | required | Kernel error messages scanned against `alma_certify/data/dmesg-patterns.conf` (deny patterns with an allowlist for known-benign firmware noise). |
| `validate.platform.pcie-aer` | required | No uncorrected PCIe AER errors. |
| `validate.platform.rtc` | required | RTC is readable, **running** (advances between two reads), and **writable** (set, verified, restored). The offset against the system clock is recorded but never gates: that offset is a matter of NTP and `/etc/adjtime`, not hardware, and is legitimately large on live media. |
| `validate.platform.reboot` | conditional, interactive | Clean reboot; the run resumes automatically via a one-shot systemd unit and verifies the boot id changed, PCI devices re-enumerated identically, and no systemd units failed. |
| `validate.platform.thermal` | conditional | Thermal zones readable and below their critical trip points. |
| `validate.platform.watchdog` | conditional | Watchdog device present and its driver identified. Never pets or fires it. |
| `validate.ipmi.bmc` | conditional | Only runs where SMBIOS type 38 declares a BMC (or the kernel already exposes an IPMI interface), so machines without one skip without probing. Loads `ipmi_si`/`ipmi_ssif`/`ipmi_devintf`, then `ipmitool mc info` must return a firmware revision and not report the controller unavailable. Records manufacturer, firmware, IPMI version, and chassis status. An interface that never comes up is a skip carrying the modprobe error, not a failure: type 38 is occasionally stale and a BMC can be disabled in firmware. |
| `validate.ipmi.health` | conditional | Chassis health as the BMC reports it, which is the SMART check for everything that is not a disk. Fails on any sensor in a critical or non-recoverable state, and on the `chassis status` fault flags (power overload, main power, power control, drive, cooling/fan). Non-critical threshold crossings are surfaced in the reason without gating. `Chassis Intrusion` never gates: it is asserted on any machine whose case has ever been opened. Unpopulated fan headers and empty PSU bays read as "no reading" and are ignored. |
| `validate.ipmi.sel` | informational | Reads the System Event Log, reports entry counts and any entries that indicate a real failure (uncorrectable ECC, thermal trip, CATERR/IERR, PSU failure, logging-limit reached). Never gates: the SEL is historical, survives OS installs, and often predates the current owner, so failing on it would condemn working hardware for something that happened before the run existed. |
| `validate.platform.selinux` | informational | Records the SELinux mode the run was performed in. Never gates: SELinux state is a boot parameter, not a hardware property, and a machine running permissive has nothing wrong with it. It is reported because it is provenance - driver probing and firmware loading do behave differently under enforcing, so a pass collected with SELinux off is weaker evidence than the same pass in the configuration AlmaLinux ships. Anything other than enforcing is flagged in the reason for a reviewer to weigh. |
| `validate.platform.secureboot` | informational | Secure Boot state and EFI/BIOS mode. |
| `validate.platform.firmware` | informational | BIOS version/date, plus fwupd device inventory when available. |

### Virtualization, management, GPU, peripherals

| Test | Severity | What it checks |
|---|---|---|
| `validate.virt.kvm` | conditional | Staged: CPU vmx/svm flag → the `kvm_intel`/`kvm_amd` module loads and `/dev/kvm` exists → boots a minimal libvirt domain from the installed kernel and confirms it reaches running. A loaded module is direct evidence that firmware allows virtualization, so the log is consulted only when `modprobe` refuses, to distinguish "disabled in BIOS" from other load failures - and then only for a line the kvm subsystem printed itself. A bare search for "disabled by BIOS" across the whole boot log failed every AlmaLinux 8 run on `x86/cpu: SGX disabled by BIOS`, which is a different feature entirely. |
| `validate.gpu.driver` | informational | Records each GPU and whether a driver is bound. Never fails on an unbound GPU: nearly every server has a BMC display adapter (Matrox G200, ASPEED AST) that drives a console nobody looks at and needs no driver, and an unbound discrete card usually means an out-of-tree driver (NVIDIA's) or a deliberate blacklist - a software choice, not a hardware defect. Unbound accelerators are flagged in the reason for a reviewer. GPU capability is enforced where it is claimed instead: `bench.gpu.*` refuses to record a metric without driver and runtime versions. |
| `validate.gpu.nvidia-smi` | required | The NVIDIA driver answers about the card. Skips unless the inventory reports an NVIDIA GPU and `nvidia-smi` is installed; where both hold, it failing means the installed driver does not match the running kernel, which is the commonest GPU fault there is. First of the NVIDIA checks so that fault is named rather than resurfacing as a confusing CUDA initialization error later. |
| `validate.gpu.cuda-devicequery` | required | The CUDA runtime enumerates the card and agrees with the driver about it: device count, compute capability, memory, clock, ECC. deviceQuery, and the thing that has to work before any of the rest means anything. Compute mode is recorded but never gates: exclusive-process is a deliberate configuration choice and the reason the *next* job on the machine fails. |
| `validate.gpu.cuda-vectoradd` | required | The card computes the right answer, checked on every element of a 16 Mi-element vector add. vectorAdd, and the only GPU check that catches a card which runs and is wrong, which is the failure mode that matters most and shows up least. |
| `validate.gpu.cuda-bandwidth` | required | Host-to-device and device-to-host transfers complete and clear a floor of 0.5 GiB/s. bandwidthTest as a validation rather than a measurement: any sane link clears the floor comfortably, so tripping it means a link trained at fewer lanes than the card expects rather than a card that is merely slower than hoped. On-card copies are reported and never gate, having nothing to do with how the card is seated. How fast it actually is belongs on a leaderboard, and `bench.gpu.cuda-bandwidth` records it from the same probe. |
| `validate.power.cpufreq` | conditional | cpufreq driver present; governor can be switched and is restored. |
| `validate.power.backlight` | conditional | Sets a display's brightness, verifies the display reports the new value, and restores it. Two routes: the kernel backlight class (`/sys/class/backlight`, an internal panel) and, when that is empty but a display is connected, DDC/CI over the monitor's I2C channel via `ddcutil` (an external monitor on a desktop, which is how a desktop brightness slider reaches it). Fails **only** when the driver refuses a value already inside `[1, max_brightness]`, which nothing in userspace can cause. Everything else is a skip with the reason: nothing plugged in, no brightness feature over DDC/CI, no `ddcutil` (EPEL 8 ships none), or a value that another process moved or that the driver reports on its own scale. Brightness is sampled across the settle window starting immediately after the write, so a laptop that dims itself mid-test is not mistaken for a broken panel. |
| `validate.power.suspend` | conditional, interactive | `rtcwake -m mem` suspend/resume; devices must re-enumerate identically. |
| `validate.usb.hotplug` | conditional, interactive | Operator plugs and removes a device; both transitions must be detected. |
| `validate.media.audio` / `.video` | informational | Sound card and DRM connector inventory. |

## Benchmarks

Benchmarks feed the public leaderboards. Every result records the metric, its
unit, and whether higher or lower is better; one metric per benchmark is
marked primary and is what leaderboards rank by. Results also capture the
tuned profile, CPU governor, SMT state, and mitigation status so comparisons
can be bucketed fairly.

| Test | Tool | Primary metric | Direction |
|---|---|---|---|
| `bench.cpu.sysbench-single` | sysbench (EPEL) | events/s | higher |
| `bench.cpu.sysbench-multi` | sysbench, all threads | events/s | higher |
| `bench.cpu.stressng-matrix` | stress-ng (EPEL) | bogo-ops/s | higher | v2 |
| `bench.mem.bandwidth` | bundled `streamish.c` | triad MB/s | higher |
| `bench.mem.latency` | bundled `memlat.c` | ns | lower |
| `bench.mem.stressng-stream` | stress-ng | MB/s (read, write) + Mflop/s | higher | v2 |
| `bench.net.iperf3-tcp` | iperf3 (needs `--peer`) | Gbit/s | higher |
| `bench.net.iperf3-reverse` | iperf3 `-R` | Gbit/s | higher |
| `bench.crypto.openssl-aes256gcm` | openssl speed | MB/s | higher |
| `bench.crypto.openssl-sha256` | openssl speed | MB/s | higher |
| `bench.crypto.openssl-rsa4096` | openssl speed | signs/s | higher |
| `bench.compress.zstd` | zstd on a generated corpus | MB/s (level 3) | higher |
| `bench.compress.xz` | xz on a generated corpus | MB/s | higher |
| `bench.compile.python` | build pinned CPython | seconds | lower |
| `bench.sched.stressng-switch` | stress-ng | ops/s + ns per switch | higher / lower | v2 |
| `bench.gpu.clpeak` | clpeak, built on the machine | 12 tests per backend, named `<backend>_<test>` | mixed |
| `bench.gpu.cuda-bandwidth` | bundled CUDA probe | GiB/s host to device | higher |

Notes:

- The memory bandwidth benchmark is **our own** triad-style kernel, not
  STREAM. STREAM's license restricts publishing modified results, so results
  must never be labeled as STREAM numbers.
- The compression corpus is generated deterministically from a fixed seed, so
  every machine compresses byte-identical input with no downloads. Which is also
  why the compression **ratio** is not published as a metric: identical input at
  a fixed level yields the same ratio on every machine, so ranking hardware by it
  ranks nothing. Three systems as different as a 4-core desktop, a 44-core
  dual-socket server, and a laptop all reported 6.76 at zstd level 19. The value
  is recorded in the result's `details` as provenance for the speed figures.
### The NVIDIA CUDA checks

Three of the four need a CUDA toolchain, and the suite will not install one. The driver and the
toolkit come from NVIDIA's own repository under their license, and a certification suite that added
a third-party repository to a machine it was pointed at would be doing something the operator did
not ask for. Absent, they skip and name what to install.

`alma-certify setup-gpu` will install what is missing, after showing you the commands and asking. It
prints the plan and exits with `--dry-run`, and takes `--yes` for a provisioning script.

A validate or benchmark run makes the same offer itself, before the tests, rather than sending you
to another command on a machine whose card is sitting there unusable. It shows what it would run and
asks; the default is no. `--gpu-setup` answers yes without asking and `--no-gpu-setup` skips the
offer, for provisioning and CI. With nobody to ask - a kickstart `%post`, a CI runner - it skips and
names the flag rather than prompting into a closed stdin. In the guided interface the question is a
dialog, and the GPU setting on the run form answers it ahead of time.

By hand, on AlmaLinux 9 and 10, which is the route the distribution supports:

```
dnf install almalinux-release-nvidia-driver
dnf install nvidia-open cuda
```

AlmaLinux 8 has no such release package, so it takes NVIDIA's own repository along with matched
kernel headers, PowerTools, EPEL for DKMS, and the `nvidia-driver:open-dkms` module stream. See
NVIDIA's [driver installation guide for
AlmaLinux](https://docs.nvidia.com/datacenter/tesla/driver-installation-guide/almalinux.html), or let
`setup-gpu` do it.

**The driver needs a reboot and the toolkit does not.** A kernel module cannot load into the kernel
that is already running, so a run that installs the driver cannot then certify the card; reboot and
run again. Where the driver already answers and only the toolkit is missing, `setup-gpu` installs
`cuda-toolkit`, which pulls no driver, and the run can carry straight on.

Nothing is offered on live media, in a container, or on a read-only root: the root will not survive
the reboot, or the kernel belongs to somebody else, so an install there costs a download and
achieves nothing.

If you would rather install only the compiler, install the whole toolkit anyway:

```
dnf install cuda-toolkit
```

`cuda-nvcc` alone cannot build anything. The runtime headers and `libcudart` come from
`cuda-cudart-devel`, and a compile that fails for want of them is reported as a skip naming the
package rather than as a broken machine, because installing only the compiler is an easy mistake
and "your toolchain is broken" is the wrong thing to tell somebody who made it.

`nvcc` does not have to be on `$PATH`. NVIDIA's RPMs install under a versioned prefix and put
nothing on it, so the suite looks in `$PATH`, then `$CUDA_HOME` and `$CUDA_PATH`, then
`/usr/local/cuda` and `/opt/cuda`, then the versioned prefixes newest first. Version order is
numeric, so a machine with both 9.0 and 13.3 installed uses 13.3.

The checks are built against the CUDA runtime API only, and read the device's clock, ECC state,
multiprocessor count, and compute mode through `cudaDeviceGetAttribute` rather than off
`cudaDeviceProp`, because CUDA 13 removed those members from the struct.

- GPU benchmarks skip when no GPU has an identified driver, or when the tool is
  not available in the configured repositories (`clpeak` availability varies by
  EPEL release and architecture). **A GPU metric is never recorded without driver
  and runtime version information.**
### The GPU compute benchmark is built on the machine under test

clpeak is in no EPEL or AlmaLinux repository, and packaging it as a binary would defeat the point:
its backends are compiled in, so one build would either omit CUDA on an NVIDIA box or carry SDK
dependencies no AMD box can satisfy. Instead the suite builds it where it runs, and gets exactly the
backends that machine's hardware supports.

Two halves, and they meet in the middle:

**At packaging time**, once, on a maintainer's machine. The spec carries the pinned release as its
own `Source1`, so nothing has to be fetched into the working tree first:

```
spectool -g -R packaging/alma-certify.spec    # downloads Source1 into SOURCES
rpmbuild -ba packaging/alma-certify.spec
```

`%global clpeak_version` in the spec is the single place the version is pinned; bump that one line
and both the download URL and `Provides: bundled(clpeak)` follow it. `%prep` deletes any
`alma_certify/data/clpeak-*.tar.gz` the source archive happened to carry, so `Source1` is the only
supplier and the package cannot ship two copies while claiming one. `%check` fails the build if it
does.

#### Building the source tarball from a working tree

For a build before there is a tag to fetch. Run this from `packaging/`:

```
tar --transform='s,^\.,alma-certify-0.1.0,' \
    --exclude=.git --exclude=build --exclude=__pycache__ \
    --exclude=.pytest_cache --exclude=.ruff_cache --exclude=.coverage \
    --exclude='*.state' --exclude='*.tar.xz' --exclude='*.tar.gz' --exclude='*.src.rpm' \
    --exclude=results_alma-certify \
    -cJf alma-certify-0.1.0.tar.xz -C .. .
```

The `-C .. .` at the end is the part that matters. Archiving `../` instead makes tar strip the
leading `../` from every member, so nothing begins with a dot for the transform to anchor on: the
prefix lands on no file at all, only the dotfiles are rewritten (`.coverage` becomes
`alma-certify-0.1.0coverage`), and the archive unpacks as an empty `alma-certify-0.1.0/` with the whole
tree sitting beside it. `%prep` detects exactly that and says which of the two shapes it got.

The excludes are the other half. Without them the archive absorbs `build/`, the mock results
directory, and the previous SRPM, and grows by tens of megabytes per build. Two checks worth running
before a build:

```
tar -tJf alma-certify-0.1.0.tar.xz | grep -cv '^alma-certify-0.1.0/'   # 0
tar -tJf alma-certify-0.1.0.tar.xz | grep -c alma_certify/data/          # 9
```

Nothing is downloaded on the machine under test. A benchmark built against whatever a branch said
that day is not comparable with one built last month, and every other data file this suite needs
ships the same way.

**On the machine under test**, per run: the suite installs the build dependencies and any vendor SDK
the cards present call for, configures with `CLPEAK_ENABLE_CPU=OFF` and nothing else, builds, then
runs **each enabled backend on its own**.

That last part is what makes the numbers mean something. A single multi-backend run prints figures
for everything it found, and a parser taking the first would record one number with nothing saying
which stack produced it, so a leaderboard would compare an NVIDIA CUDA figure against an AMD OpenCL
one. Metrics are therefore named per backend (`cuda_single_precision_compute`,
`opencl_single_precision_compute`), with `backends_built`, `backends_not_built`, and `per_backend`
in the result details.

### Which of clpeak's tests run, and which are recorded

**All of them run.** clpeak's default is every test the backend supports and the suite passes no
test-selection flags, so a CUDA backend runs fourteen tests and an OpenCL one similar. Each is
bounded by clpeak's own `--max-time` (500 ms of timed phase), so running everything is close to
free next to the build.

**Twelve are recorded**, by clpeak's canonical tag: single, double, half, mixed, and bfloat16
precision compute; integer and int8 dot product; global, local, and image memory bandwidth; host
transfer bandwidth; and kernel launch latency. These are the portable set, so a metric name means the
same measurement wherever it came from. Eleven of the twelve are on all five backends; `bfloat16` is
on CUDA, Vulkan, ROCm, and oneAPI but not OpenCL, which simply produces no row there.

The rest are deliberately not promoted to metrics. clpeak reports twenty-three tests on CUDA once
the matrix-engine work is counted - sixteen of them `wmma_*` variants, plus cuBLAS,
rocBLAS, rocWMMA, MFMA, coopmat, and oneMKL - and those are vendor-specific micro-benchmarks whose
numbers mean nothing across vendors. **Nothing is lost:** clpeak's own JSON goes into the run's
artifacts in full, so anything worth promoting later is already collected.

Results are read from `--json-file`, not scraped from the terminal output. Two reasons, both learned
the hard way here: a layout is not an interface, and a pattern can only find what it was written
for. The scraping version recorded three of the twelve tests, and one of the three was the wrong
number - the patterns matched the first `float :` line after each heading, which is the *narrowest*
vector width, where clpeak exists to report the peak. On OpenCL, where compute runs at five widths,
the scalar figure was being published as the card's single-precision compute. What is recorded now
is the peak across widths and across devices, with the winning width and device beside it.

`kernel_launch_latency` is in microseconds and is the one GPU metric where **lower is better**.

Every `CLPEAK_ENABLE_<BACKEND>` option defaults to ON and all of them except OpenCL are auto-detected
by clpeak's own CMake, so there is no backend selection logic here: installing the SDK is what
enables a backend. Two exceptions worth knowing, because both fail the whole build rather than one
backend: OpenCL has no detection at all, and Vulkan's shader step aborts when `glslc` is missing.

One release-specific gap: **AlmaLinux 9 cannot build clpeak** as things stand. Its `opencl-headers`
does not ship `CL/opencl.hpp`, nothing in 9 or EPEL 9 provides it, and clpeak's CMake responds to
that by git-cloning the Khronos OpenCL SDK at an unpinned branch and building it during configure.
The suite refuses rather than allow that, so 9 skips with a reason until the header is available,
either vendored beside the tarball or packaged.

- **What the GPU benchmarks cover, and what they do not.** clpeak is not the
  OpenCL-only tool it once was: current versions run OpenCL, CUDA, ROCm/HIP,
  Vulkan, and oneAPI/SYCL, and by default run every test on every backend present.
  One package therefore reaches every vendor runtime this program cares about.

  What it does not reach is **graphics**. Its Vulkan support is a compute
  backend, cooperative-matrix tests rather than rasterization, so nothing here
  exercises the vertex, texture, blend, or present path. That is deliberate: this
  program certifies that hardware works on AlmaLinux, and for the machines it
  sees that means compute. hashcat, glmark2, and vkmark were removed rather than
  packaged, and none of the four was in EPEL or in any AlmaLinux repository, so
  clpeak is being packaged either way.

  One thing to know if graphics ever comes into scope: **AlmaLinux 9 aarch64
  ships no Vulkan ICD at all**, so a Vulkan benchmark is unrunnable there however
  well it is packaged, and clpeak's Vulkan backend is inert on that platform too
  while its OpenCL backend still works.

## Adding a test

1. Subclass `Test` (or `BenchmarkTest`) in the appropriate module under
   `alma_certify/validate/` or `alma_certify/benchmarks/`.
2. Set `id`, `category`, `run_type`, `severity`, `default_timeout`, and any
   `packages`/`repos` the test needs.
3. Implement `applicable(ctx)` to return a skip reason when the hardware or
   prerequisite is absent - skipping is normal and is not a failure.
4. Implement `run(ctx)` returning `self.result(...)`. Use `ctx.cmd()` so
   output is captured to an artifact and the timeout is enforced, and
   `ctx.artifact_path()` for any file you write.
5. Register it: `REGISTRY.register(MyTest)`, and add the module to
   `VALIDATE_MODULES` / `BENCHMARK_MODULES` in `alma_certify/registry.py` if it is
   a new file.
6. Add parser tests under `tests/` using captured tool output as fixtures.

### A note on stress-ng versions

The three stress-ng benchmarks are at `benchmark_version = "2"`. Version 1
parsed the metrics table by column position and every one of them counted a
column short, publishing the stressor's **sys time in seconds** under a
throughput unit: 0.13 bogo-ops/s for matrix, and 33 MB/s for stream on a machine
whose bundled bandwidth micro measured 52,682 MB/s. `bench.mem.stressng-stream`
was doubly wrong, because its primary pattern looked for `MB/sec` where
stress-ng writes `MB read/sec`, so it never matched on any release and always
fell through to that column.

Columns are now read by name in `alma_certify/benchmarks/stressng.py`, once, because
three separate positional regexes are how one mistake got made three times.
Version 1 results are not a less precise reading of the same quantity and must
not share a leaderboard with these, which is what the version bump enforces.
