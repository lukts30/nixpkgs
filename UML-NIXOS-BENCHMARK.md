# NixOS tests on UML: ptrace versus seccomp — 2026-10-28

## Reproduce

```sh
nix-build -A user-mode-linux.benchmarks.nixos -o result-uml-nixos-benchmark
./result-uml-nixos-benchmark/bin/uml-nixos-benchmark \
  --cases tmpfs-ptrace,tmpfs-seccomp,block-ptrace,block-seccomp \
  --trials 3 --cpus 0,1,2,3,4,5,6,7 /tmp/uml-nixos-seccomp-results
```

Choose physical cores appropriate to the host and a new output directory.
This needs unprivileged user/mount/network namespaces; neither root nor KVM.
Use `--cases tmpfs-ptrace,tmpfs-seccomp` for the smaller comparison.
[UML.md](UML.md#select-seccomp-execution-instead-of-ptrace) covers the option,
individual Nix test builds and interactive use.

## Results

All four Nix test derivations and all 16 benchmark invocations passed (four
warmups plus 12 measured runs). Medians of three measured runs:

| Root | Execution | Full test wall time | Range | Host CPU time | Context switches |
|---|---|---:|---:|---:|---:|
| tmpfs | ptrace | 54.16 s | 53.03–57.13 s | 62.15 s | 6.86 M |
| tmpfs | seccomp | 48.00 s | 42.41–49.23 s | 52.64 s | 4.66 M |
| qcow2/ext4 | ptrace | 81.98 s | 79.03–83.96 s | 87.02 s | 9.41 M |
| qcow2/ext4 | seccomp | 67.10 s | 60.92–70.99 s | 68.72 s | 6.30 M |

Seccomp reduced median wall time by **11.4%** for tmpfs and **18.2%** for block
roots; CPU time fell **15.3%** and **21.0%**. Context switches fell about a third.
These are small samples on a shared workstation, with polling and service waits;
they demonstrate a benefit for this test, not a universal speedup.

[Raw measurements, configuration and boot/QMP validation](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-nixos/)
retain every trial, including excluded warmups. Each expected boot reported the
selected ptrace/seccomp probe successfully; QMP reported zero guest CPUs for every
block backend. Full console logs remain in the output directory shown above.

## Method

- The original two-machine Git daemon test runs unchanged, with the existing UML
  additions for passt TCP/UDP and client/backend restart. Both guests explicitly
  use either `seccomp=off` or `seccomp=on`. The test checks the host-side UML
  command line before and after client restart; `on` cannot silently fall back.
- The block-root variant additionally checks ext4 `/dev/vda`, writeback negotiation
  before fsck, hostfs store access, file transfers, persistence, and server/backend
  restart. QEMU `-machine none` supplies each qcow2 block export; it has no guest
  CPU. Compare modes **within each root type**: the block test does more work.
- All drivers are built before timing. One complete warmup per configuration is
  excluded, followed by three trials per configuration in shuffled order (seed 42).
  Runs are sequential. Each invocation gets fresh machine state and guest roots;
  host store caches are not dropped.
- Wall time covers the complete driver invocation: setup, NixOS initrd and stage 2
  boots, assertions, restarts, shutdown and driver cleanup. This measures test-suite
  turnaround, not network throughput. Existing service waits and polling are included.
- CPU time is host `getrusage(RUSAGE_CHILDREN)`, user plus system time, for the
  driver and its waited-for descendants, including UML and device backends.
  Detached/unreaped descendants and asynchronous host kernel work are not counted.
- Ryzen 7 5800X, Linux 6.18.57 host, UML 6.18.59; affinity CPUs 0–7 (eight physical
  cores), not reserved, frequency scaling enabled. Each UML guest has a 1 GiB RAM
  limit. RAM files and machine state, including qcow2 images, live on a private
  tmpfs. The Nix store remains hostfs. This avoids measuring a disk-backed UML RAM
  file; the block-root results are not SSD benchmarks.

**Boot path in these measurements:** both root types used an initrd: kernel → systemd initrd/stage 1 → NixOS stage-2 activation → switch_root → systemd.
Seccomp changes the execution mechanism, not that sequence. The later
[direct-boot comparison](UML-BOOT-BENCHMARK.md) covers the optional initrd-free path. The standalone
BusyBox block-root tests are separate and boot directly without an initrd.
The existing nspawn test backend shares the host kernel, prepares a root
directory and mounts, then directly executes `${system.build.toplevel}/init`.
It starts NixOS stage 2 without a guest kernel boot or initrd/stage 1; that is
part of what a future nspawn startup comparison would measure. See
[the nspawn launcher](nixos/modules/virtualisation/nspawn-container/run-nspawn/src/run_nspawn/__init__.py)
and [its NixOS module](nixos/modules/virtualisation/nspawn-container/default.nix).

The package retains ptrace as its default. Upstream marks UML's seccomp execution
mode as suitable only for trusted userspace; it is not a replacement for service
seccomp policy, and it does not enable SMP. See the
[option documentation](https://github.com/torvalds/linux/blob/v6.18/arch/um/os-Linux/start_up.c)
and the separate [CPU/syscall benchmark](UML-CPU-BENCHMARK.md).

## Useful next comparisons

These are proposed follow-ups, not additional verified results:

1. **The same service tests under systemd-nspawn, UML and QEMU/KVM.** Measure
   startup-to-ready, peak aggregate PSS and tests/hour at 1/4/8 concurrent tests.
   NixOS already has a [`containers` backend](nixos/lib/testing/nodes.nix).
   This would show the practical cost of using a separate guest kernel, and how
   well multiple independent UML instances use the host's cores.
2. **Fork/exec and minor-page-fault workloads.** Add process-launch rate,
   anonymous-memory first-touch, and `mmap`/`munmap` tests across ptrace, seccomp,
   native, QEMU and gVisor. UML's source specifically identifies fewer context
   switches on minor faults as a reason for the seccomp path; `gettid` alone does
   not measure that. Then confirm the effect on a process-heavy build or service.
3. **KUnit and Rust kernel tests.** Use UML as an unprivileged kernel CI target,
   compare suite turnaround with QEMU, and test a separate KASAN/KCOV configuration.
   [KUnit uses UML by default](https://kunit.dev/), and
   [Rust doctests can run through KUnit](https://docs.kernel.org/rust/testing.html).
   The current packaged configuration leaves KUnit, KASAN and KCOV disabled.
4. **Time-travel mode for timer/expiry tests.** UML can skip idle virtual time or
   coordinate multiple instances with an external scheduler. This could shorten
   long retry, TTL and lease tests. It requires a separate kernel configuration
   (`UML_TIME_TRAVEL_SUPPORT` is currently disabled) and compatible external I/O
   coordination; ordinary passt is not proof of deterministic simulated networks.
   See [the upstream modes and constraints](https://github.com/torvalds/linux/blob/v6.18/arch/um/kernel/time.c#L960).
