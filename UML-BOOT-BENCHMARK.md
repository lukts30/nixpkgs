# UML boot: initrd and direct stage 2

## Select a mode

```nix
umlDefaults.virtualisation.uml = {
  bootMode = "direct"; # default: "initrd"
  # Direct boot enables rootDisk by default; it requires the ext4 block root.
  seccomp.enable = true; # optional; ptrace remains the default
};
```

Both boot a separate UML kernel and use the same qcow2/ext4 seed, exported over
vhost-user-blk by QEMU `-machine none -nodefaults`. `/nix/store` and `/tmp/shared`
use hostfs. The seed contains a static BusyBox and a small bootstrap, not the
NixOS closure.

- **Initrd:** kernel → systemd initrd → NixOS activation → switch_root → systemd.
- **Direct:** kernel mounts ext4 read-only → on-disk bootstrap mounts the store,
  enables disk writeback, runs `e2fsck -p`, remounts root writable, and prepares
  special filesystems/device links → NixOS stage 2 activation → systemd.
- **nspawn:** host kernel → nspawn prepares the directory/mounts → NixOS stage 2.
  It uses no guest initrd and boots no separate kernel.

Direct boot requires built-in ext4 and virtio root drivers; it does not execute
NixOS initrd services. A filesystem check that requests reboot or recovery stops
boot. Initrd mode remains available for tmpfs roots and customized stage 1.
These tests start from clean images and shut down normally. Use initrd mode for
unclean-image recovery: direct boot mounts ext4 before userspace can configure
writeback, so kernel journal replay precedes that setup.

## Rerun

```sh
# Existing two-machine Git daemon test: TCP, UDP, transfers, persistent roots,
# and guest/network/storage backend restarts. No KVM or host root needed.
nix-build -A nixosTests.user-mode-linux.gitdaemon-direct -o result-uml-direct
nix-build -A nixosTests.user-mode-linux.gitdaemon-direct-seccomp -o result-uml-direct-seccomp

# Interactive use: start_all(), client.succeed(...), server.succeed(...).
nix-build -A nixosTests.user-mode-linux.gitdaemon-direct.driverInteractive -o result-uml-direct-driver
./result-uml-direct-driver/bin/nixos-test-driver -o /tmp/uml-direct-logs

# Prebuild the drivers, then run a balanced boot-mode/execution-mode comparison.
nix-build -A user-mode-linux.benchmarks.nixos -o result-uml-nixos-benchmark
./result-uml-nixos-benchmark/bin/uml-nixos-benchmark \
  --cases block-ptrace,block-seccomp,direct-ptrace,direct-seccomp \
  --trials 3 --cpus 0,1,2,3,4,5,6,7 /tmp/uml-boot-results
```

Choose available physical cores and a new output directory. Start with fresh
driver state if an older retained image lacks the on-disk bootstrap. This requires
unprivileged user/mount/network namespaces. RAM files and machine state live on
a private tmpfs. Each invocation starts with fresh roots; one warmup per case
is excluded and three measured trials are shuffled with seed 42. Drivers are prebuilt and the timed command performs no Nix builds. Other host
work is not isolated. The host and CPU accounting match the
[seccomp comparison](UML-NIXOS-BENCHMARK.md#method).

## Results — 2026-10-28 UTC

All four Nix test variants passed, as did the default lighttpd regression and
NixOS manual build. The final benchmark passed **16 invocations / 64 boots**:
four excluded warmups and twelve measured trials. Every invocation also checks
root ownership of the bootstrap files, execution/boot mode, networking and
persistent root data. QMP confirms zero guest CPUs in every storage backend.

Seconds below are medians of three trial means. Each initial mean covers the
two concurrently booting guests; each restart mean covers their two later,
individual reboots. Milestones are cumulative from before backend startup.

| Boot / execution | Kernel init | Activation done | Systemd ready | Initial-ready range | Restart ready |
|---|---:|---:|---:|---:|---:|
| Initrd / ptrace | 0.38 | 7.99 | **20.37** | 19.63–20.88 | 18.36 |
| Direct / ptrace | 0.22 | 1.70 | **13.69** | 13.54–14.49 | 11.97 |
| Initrd / seccomp | 0.42 | 5.91 | **15.23** | 15.16–54.02 | 14.26 |
| Direct / seccomp | 0.23 | 1.25 | **10.33** | 10.33–10.46 | 9.12 |

Direct boot reduced median initial readiness time by **32.8% with ptrace** and
**32.1% with seccomp**. Most of the saving is before activation completes; the
following service startup still takes roughly 9–12 seconds in the usual runs.

| Boot / execution | Full test median (range), s | Host CPU median, s |
|---|---:|---:|
| Initrd / ptrace | 79.22 (77.62–95.26) | 84.14 |
| Direct / ptrace | 59.32 (58.41–69.92) | 62.65 |
| Initrd / seccomp | 61.79 (61.28–129.41) | 63.83 |
| Direct / seccomp | 46.73 (46.64–46.86) | 48.23 |

Full-test medians fell about **25%** in both execution modes. This includes
assertions, transfers, service waits, four boots, shutdown and cleanup.

**Variability matters:** the initrd/seccomp outlier reached readiness at 54 s
and completed the test at 129 s. A later host-load check showed a 1-minute load
of 8.05, but there was no continuous host-load recording to establish causality.
No measured trials were discarded. These are small samples on a shared
workstation, so treat the medians as indicative rather than guaranteed speedups.

[Raw measurements, all boot traces, QMP evidence and validation](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-boot/)
include warmups. `summary.json` contains initial-boot and whole-test statistics;
`boot-summary.json` also derives restart medians from `measurements.json`.
Full console logs remain under `/tmp/uml-boot-results`.

## Trace interpretation

`<machine>-boot.jsonl` contains host monotonic observation times measured from
before backend startup, with a new boot number on every restart:

- `kernel-exec`: the storage/network backends are ready; about to launch UML.
- `kernel-init`: the kernel announces execution of `/init` or `/uml-init`.
- `stage2-activation-done`: ordinary NixOS stage-2 activation has completed.
- `systemd-ready`: systemd reports startup complete. This is not an HTTP or Git
  readiness assertion; the test separately verifies the actual service.

Direct boots also record bootstrap, writeback, fsck and stage-2-entry markers;
initrd boots record switch_root. All times include scheduling and console delivery
latency. The initial two guests boot concurrently; restarts happen individually.
Report initial boot times separately from restarts. Systemd's own timing string
is retained, but its phase labels are not directly comparable: without an initrd,
it counts the pre-systemd bootstrap/activation time in its “kernel” interval.
