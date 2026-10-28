# UML CPU scaling and syscall overhead — 2026-10-28

UML is close to native speed for one arithmetic worker, but eight threads still
share one guest CPU. Its default ptrace path makes tiny syscalls about **44–50×
slower than native** in this run. gVisor scales across cores, including its
ptrace platform; syscall interception and guest SMP are separate limitations.

## Results

Medians of three trials. Compute throughput is **million iterations/second**,
summed across workers; higher is better. Speedup compares eight threads with one
in the same runtime. Host cores means CPU-seconds per elapsed second.

| Runtime | 1 thread | 2 threads | 4 threads | 8 threads | Speedup | Host cores, 8 threads |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Native | 774.8 | 1,531.6 | 3,001.9 | 5,748.8 | 7.42× | 7.71 |
| UML ptrace (default) | 762.1 | 762.7 | 774.1 | 777.2 | 1.02× | 0.99 |
| UML seccomp | 780.4 | 781.8 | 781.8 | 779.8 | 1.00× | 1.00 |
| QEMU/KVM, 1 vCPU | 778.2 | 777.6 | 778.9 | 778.6 | 1.00× | 1.01 |
| QEMU/KVM, 8 vCPUs | 774.0 | 1,512.5 | 3,007.8 | 5,841.5 | 7.55× | 7.88 |
| gVisor systrap (default) | 774.6 | 1,539.1 | 3,035.2 | 5,779.0 | 7.46× | 7.73 |
| gVisor ptrace | 782.7 | 1,551.9 | 3,009.7 | 5,663.6 | 7.24× | 7.57 |
| gVisor KVM | 783.0 | 1,553.5 | 3,039.1 | 5,944.8 | 7.59× | 7.90 |

Eight-thread native throughput is **7.4× UML's**; single-thread UML is only
**1.6% below native** here. The one-vCPU QEMU control also stays flat, illustrating
the effect of a single guest CPU independently of UML's execution mechanism.

Single-worker syscall cost in **microseconds/call**; lower is better. Context
switches and host cores are for `gettid`, across the complete runtime process tree.

| Runtime | `gettid` µs | Futex wake µs | Host cores | Host context switches/call |
| --- | ---: | ---: | ---: | ---: |
| Native | 0.262 | 0.306 | 0.99 | <0.001 |
| UML ptrace | 13.201 | 13.500 | 0.93 | 2.004 |
| UML seccomp | 9.355 | 9.720 | 1.00 | 2.003 |
| QEMU/KVM, 1 vCPU | 0.138 | 0.203 | 1.01 | <0.001 |
| QEMU/KVM, 8 vCPUs | 0.113 | 0.152 | 1.01 | <0.001 |
| gVisor systrap | 3.167 | 3.251 | 2.01 | 0.011 |
| gVisor ptrace | 14.268 | 14.453 | 0.94 | 2.025 |
| gVisor KVM | 0.654 | 0.651 | 1.00 | 0.001 |

UML seccomp reduces `gettid` latency by **29%**, but retains about two host context
switches per call and does not add SMP. gVisor systrap is **4.2× faster** than
default UML on `gettid`, using approximately two host cores for one worker;
gVisor KVM is **20.2× faster**, using about one core. gVisor ptrace has costs
similar to UML ptrace despite supporting parallel compute threads.

This measures the complete syscall paths, not a standalone ptrace instruction's
cost. Native and QEMU run different kernel configurations and expose different
CPU/mitigation environments; QEMU's lower syscall numbers do not establish that
virtualization makes arbitrary applications faster. No mitigations were disabled.
Results vary on this working desktop: UML ptrace `gettid` ranged from
**51.6–77.3 thousand calls/s**, versus its median **75.8 thousand**. Native
eight-thread compute ranged from **5,556–5,902 million iterations/s**. Full ranges
are retained in the raw summary; these microbenchmarks do not predict whole
service, memory-intensive, filesystem or network performance.

## Rerun

```sh
nix-build -A user-mode-linux.benchmarks.cpu -o result-uml-cpu-benchmark
./result-uml-cpu-benchmark/bin/uml-cpu-benchmark \
  --trials 3 --seconds 2 --threads 1,2,4,8 \
  --cpus 0,1,2,3,4,5,6,7 /tmp/uml-cpu-results

# Separate validation: tracing changes performance; do not compare its timings.
./result-uml-cpu-benchmark/bin/uml-cpu-benchmark \
  --cases uml-ptrace,uml-seccomp --workloads gettid --trace-uml \
  --trials 1 --seconds 0.3 --cpus 0,1,2,3,4,5,6,7 /tmp/uml-cpu-trace
```

Use new output directories and choose physical cores appropriate to your host.
Requires Linux and unprivileged user/mount namespaces. QEMU and gVisor KVM need
access to `/dev/kvm`; omit those cases with
`--cases native,uml-ptrace,uml-seccomp,gvisor-systrap,gvisor-ptrace` without KVM.
`--syscall-threads 1,2,4,8` also exercises concurrent syscall throughput; the
recorded syscall comparison uses one worker. Logs and JSON results are retained;
runtime processes and temporary RAM files are cleaned up.

## What is compared

The **same compiled C executable** runs directly on the host, in UML, in QEMU/KVM
with one or eight vCPUs, and in rootless gVisor `runsc do` using systrap, ptrace
or KVM. QMP verifies KVM acceleration and vCPU counts. UML and QEMU boot the same
BusyBox initramfs with a 512 MiB RAM limit; this is not a complete NixOS boot or
a storage test. gVisor executes the application with its userspace kernel; it
does not boot Linux or demonstrate NixOS test-runner compatibility. `runsc do`
uses a read-only host filesystem with a temporary writable overlay, without
Docker images or networking.

- Host: Ryzen 7 5800X, Linux 6.18.57; guest kernels 6.18.59, QEMU 11.1.2,
  gVisor 20260406.0. All processes inherit affinity to eight distinct physical
  cores, CPUs 0–7. These cores are not reserved; frequency scaling remains on.
- Three trials, shuffled case order. Each phase warms up for 0.2 seconds per
  active worker, waits for the controller, then measures at least two seconds.
  Boot, thread creation, warmup and shutdown are excluded. Eight persistent
  pthreads are created; only the requested count does work in each phase.
- Compute: dependent 64-bit xorshift arithmetic, with a consumed checksum.
  One operation is one three-shift/xor iteration. Time is checked every
  4,194,304 iterations to keep UML's clock syscall from dominating computation.
- Syscalls: explicit `syscall(SYS_gettid)` and `FUTEX_WAKE_PRIVATE` with no
  waiters, checking every return value. These bypass libc/vDSO and uncontended
  pthread fast paths. Time is checked every 4,096 calls. Final batches and
  barrier completion are included in elapsed time.
- Throughput is total operations divided by probe wall time. Host CPU is summed
  scheduler runtime for the entire runtime process tree, including UML helpers
  and gVisor Sentry/gofer threads. Counters are sampled every 50 ms; short-lived
  helpers can evade sampling. Controller and asynchronous host kernel work are
  excluded. Host context switches count both voluntary and involuntary switches.
  Guest `getrusage` is retained separately, since its accounting differs by runtime.

## Why one UML guest cannot scale across CPUs

Linux 6.18's [UML Kconfig](https://github.com/torvalds/linux/blob/v6.18/arch/um/Kconfig)
fixes `NR_CPUS` to `1`; the runner checks that UML reports one online CPU.
Multiple guest threads can exist and take turns, but cannot execute concurrently
on separate guest CPUs. Separate UML instances can run on different host cores;
that does not provide SMP inside one instance. Host I/O helpers and device
backends can also occupy additional cores without providing guest SMP.

UML's default here is ptrace. The optional `seccomp=on` execution path exchanges
state through a signal handler and shared memory instead of ptrace register
operations. Upstream explicitly restricts this mode to **trusted userspace**:
its filter does not yet isolate all guest physical memory. This benchmark opts
in only for its own probe; the package/test-runner default is unchanged.
See the [kernel's option definition](https://github.com/torvalds/linux/blob/v6.18/arch/um/os-Linux/start_up.c).

gVisor's default is systrap, which uses seccomp interception; its ptrace platform
is retained here as a comparison but is no longer supported upstream.
See the [gVisor platform guide](https://gvisor.dev/docs/architecture_guide/platforms/).

## Tracing and evidence

All **24 runtime runs / 144 measured phases** completed. The controller verified
one online CPU in every UML instance and QMP confirmed the QEMU vCPU counts and
KVM acceleration. Probe and controller phase times differed by at most 8 ms.

A separate `strace -c -e trace=ptrace,wait4,futex` run, tracing only the UML kernel
process, recorded **48,830 ptrace calls and 10,480 wait4 calls** for ptrace mode.
Seccomp mode recorded **43,145 futex calls**, only 17 wait4 calls and 16 failed
ptrace calls. These counts include boot, warmup and shutdown, and tracing changes
how many benchmark operations fit in the timed phase. They confirm the distinct
execution paths; they are not the performance measurements above. The ptrace
path's register transfers and `PTRACE_SYSEMU`/wait loop are visible in
[the upstream implementation](https://github.com/torvalds/linux/blob/v6.18/arch/um/os-Linux/skas/process.c).

Retained evidence:
[measurements](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-cpu/measurements.json),
[CSV](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-cpu/measurements.csv),
[summary and ranges](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-cpu/summary.json),
[host and command metadata](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-cpu/metadata.json),
[validation](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-cpu/validation.json),
[trace summaries](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-cpu/trace-validation.json).
