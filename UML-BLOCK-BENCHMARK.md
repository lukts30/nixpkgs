# UML and QEMU block benchmark — 2026-10-28

With four guests and the same qcow2 backend, KVM delivered much higher random-I/O
throughput. Sequential I/O was close when bypassing the host cache. UML used
less CPU per GiB and about 57% less process PSS in these four-guest comparisons.

## Rerun

```sh
nix-build -A user-mode-linux.benchmarks.block -o result-uml-block-benchmark
./result-uml-block-benchmark/bin/uml-block-benchmark \
  --guests 1,4 --trials 3 --seconds 5 --size-mib 128 --memory-mib 512 \
  --cpus 0,1,2,3,4,5,6,7 /tmp/uml-block-results
```

Use a new output directory; omit `--cpus` or adapt it to the host. Requires
Linux, unprivileged user/mount namespaces and access to `/dev/kvm`. Only temporary
images created by the runner are written. Logs and measurements remain in the
output directory; guests, backends and temporary images are cleaned up.

For the separate flush validation, add `--trace-flush` and use
`--guests 1 --trials 1 --seconds 0.3 --size-mib 16`. Traced runs are unsuitable
for performance comparisons.

## Setup

| Case | Guest and device | Image / backend |
| --- | --- | --- |
| `uml-ubd-writeback` | UML native UBD | Raw image, host buffered I/O |
| `uml-vhost-writeback` | UML virtio-blk over Unix socket | qcow2, QEMU vhost-user-blk, host buffered I/O |
| `kvm-vhost-writeback` | QEMU/KVM, vhost-user-blk PCI | Same qcow2/backend configuration |
| `uml-vhost-none` | UML virtio-blk over Unix socket | qcow2, QEMU vhost-user-blk, host `O_DIRECT` |
| `kvm-vhost-none` | QEMU/KVM, vhost-user-blk PCI | Same qcow2/backend configuration |

Each vhost guest has a separate QEMU storage process using
`-machine none -nodefaults`, with **zero guest CPUs** checked through QMP.
The KVM comparison adds a QEMU `q35,accel=kvm` VM with **one vCPU**, confirmed
through QMP; it uses `memory-backend-memfd,share=on`. UML uses its normal shared
RAM file on a private tmpfs. Both guest kernels are Linux 6.18.59, with their
respective UML/native configurations, and boot the **same initramfs and C probe**.
Each guest has 512 MiB RAM. This compares whole guest stacks, not just transports.

- Host: Ryzen 7 5800X, 32 GB RAM, Linux 6.18.57; QEMU 11.1.2. Affinity is eight
  physical cores (CPUs 0–7). Cores are not reserved; frequency scaling is enabled.
- Host data: Btrfs with `compress=zstd:1`, Lexar NM790 NVMe SSD. Each guest gets
  a fresh, fully populated 128 MiB image with the same random data. Images are
  independent, unlike the shared input file in the filesystem benchmark.
- The probe operates on the **unmounted block device**, with guest `O_DIRECT`,
  aligned buffers, one synchronous syscall at a time (application queue depth 1).
  No ext4 or guest file-data cache is involved. This measures block I/O; the separate
  [block-root tests](UML.md) cover booting an ext4 root and persistent files.
- Sequential requests are 1 MiB; random requests are uniform 4 KiB. Each of four
  read/write phases runs for at least five seconds, checking time every 16 I/Os.
  Writes include a final `fdatasync`; this is not a per-write synchronous test.
  Read-ahead is disabled and the guest scheduler is `none`.
  UBD has a 127 KiB maximum request size and splits the 1 MiB calls internally;
  virtio reports 4096 KiB. UBD also uses raw images, so it is an additional
  baseline, not an isolated comparison of virtio transports. Virtio's request
  limits are 256 for UML and 128 for KVM, well above application queue depth 1.
- Host `writeback` cases warm the image cache before boot. `none` sets the QEMU
  file node's `cache.direct=true`; flushes remain enabled in both modes.
  The small dataset and short runs do not measure sustained SSD performance
  after its internal caches are exhausted.
- QEMU's export initially advertises guest `wce=0`. The runner explicitly sets
  the guest's virtio `cache_type` to `write back`, then verifies that Linux
  reports a writeback queue and completes a flush in every write phase.
  A separate traced run checks successful backend `fsync`/`fdatasync` calls.
- Three trials for each configuration and guest count, shuffled with seed 42.
  Throughput is aggregate transferred bytes / host wall time, from releasing
  all serial barriers until all guests report completion. Boot, image creation,
  shutdown and image checks are excluded. Every qcow2 image is checked afterward.
- CPU sums per-thread scheduler runtime of the guest process, UML helpers and
  storage backend. It includes guest execution and their host syscalls, but
  excludes the controller and asynchronous host kernel workers. PSS apportions
  shared RAM mappings, avoiding duplicate counting between guest and backend.
  Host image-cache residency is measured separately using `mincore`; PSS plus
  that cache still excludes other unmapped caches and host kernel bookkeeping.

## Results

Median of three trials; throughput is **aggregate MiB/s**, higher is better.

| Guests | Device / host cache | Sequential read | Random 4 KiB read | Sequential write | Random 4 KiB write |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | UML UBD / writeback | 2,148.5 | 88.9 | 1,011.4 | 26.6 |
| 1 | UML virtio / writeback | 3,439.5 | 15.8 | 890.0 | 14.7 |
| 1 | KVM virtio / writeback | 7,673.3 | 150.4 | 1,228.8 | 125.0 |
| 1 | UML virtio / none | 561.8 | 10.9 | 456.7 | 12.2 |
| 1 | KVM virtio / none | 1,188.7 | 43.6 | 605.5 | 44.2 |
| 4 | UML UBD / writeback | 8,247.2 | 270.1 | 2,721.9 | 131.1 |
| 4 | UML virtio / writeback | 10,236.7 | 79.6 | 3,051.2 | 47.8 |
| 4 | KVM virtio / writeback | 14,690.9 | 384.7 | 3,967.3 | 279.2 |
| 4 | UML virtio / none | 1,668.8 | 44.8 | 1,510.3 | 51.1 |
| 4 | KVM virtio / none | 1,787.9 | 146.9 | 1,569.0 | 123.9 |

Median **CPU-seconds/GiB**, guest plus backend; lower is better.

| Guests | Device / host cache | Sequential read | Random 4 KiB read | Sequential write | Random 4 KiB write |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | UML UBD / writeback | 0.35 | 10.19 | 0.99 | 11.18 |
| 1 | UML virtio / writeback | 0.24 | 15.52 | 1.22 | 21.22 |
| 1 | KVM virtio / writeback | 0.30 | 12.51 | 1.26 | 15.23 |
| 1 | UML virtio / none | 0.29 | 20.99 | 1.72 | 28.25 |
| 1 | KVM virtio / none | 0.31 | 32.36 | 1.74 | 36.25 |
| 4 | UML UBD / writeback | 0.38 | 10.58 | 1.05 | 11.37 |
| 4 | UML virtio / writeback | 0.29 | 15.93 | 1.23 | 21.81 |
| 4 | KVM virtio / writeback | 0.44 | 17.10 | 1.30 | 22.16 |
| 4 | UML virtio / none | 0.30 | 22.12 | 1.73 | 30.07 |
| 4 | KVM virtio / none | 0.37 | 37.91 | 1.82 | 47.57 |

Median **MiB after reads**, across all guests and backends. PSS includes the
guest runtime, not just storage overhead; each guest still has a 512 MiB RAM limit.
Each column is medianed independently; the last column sums PSS and cache per run.

| Guests | Device / host cache | Baseline PSS | PSS after reads | Host image cache | PSS + image cache |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | UML UBD / writeback | 75.7 | 75.7 | 128.0 | 203.7 |
| 1 | UML virtio / writeback | 112.2 | 112.2 | 128.3 | 240.5 |
| 1 | KVM virtio / writeback | 241.2 | 241.3 | 128.3 | 369.6 |
| 1 | UML virtio / none | 112.2 | 112.2 | 1.2 | 113.4 |
| 1 | KVM virtio / none | 241.0 | 241.1 | 1.2 | 242.3 |
| 4 | UML UBD / writeback | 283.9 | 284.0 | 512.0 | 796.0 |
| 4 | UML virtio / writeback | 358.1 | 358.4 | 513.2 | 871.6 |
| 4 | KVM virtio / writeback | 839.8 | 840.1 | 513.2 | 1,353.3 |
| 4 | UML virtio / none | 358.3 | 358.6 | 6.4 | 364.4 |
| 4 | KVM virtio / none | 847.8 | 848.1 | 5.9 | 852.9 |

No measured process swap occurred. Guest cache growth was at most 16 KiB across
a cohort. PSS stayed essentially flat through reads and writes; the sampled peak
was at most 0.07 MiB above the phase-end reading. The larger KVM PSS footprint
was already present before I/O, so it is chiefly a baseline difference in these
guest stacks. Host caching is substantial, but guest `O_DIRECT` avoids the extra
guest data-cache copies seen in the filesystem benchmark.

## Interpretation and limits

For **four guests using the same qcow2 backend**:

- Host writeback caching: KVM had **1.44× sequential-read, 4.83× random-read,
  1.30× sequential-write and 5.84× random-write throughput** compared with UML.
- Host-cache bypass: KVM was only **7% faster on sequential reads and 4% on
  sequential writes**, but **3.28×/2.42× faster on random reads/writes**.
- UML used **34% less CPU/GiB for cached sequential reads**; cached random I/O
  and writes cost within 8% of KVM per GiB. With host-cache bypass, UML used
  **42%/37% less CPU/GiB for random reads/writes**, trading throughput for CPU.
- UML's process PSS was about **358 MiB**, versus **840–848 MiB** for KVM,
  including each backend. Adding the host image cache gives approximately
  **872 versus 1,353 MiB** with caching, or **364 versus 853 MiB** without it.

UBD was faster and cheaper than UML vhost-user for random I/O in this run, but
uses raw images and a different request-splitting path. Its numbers do not
isolate the cost of qcow2 or vhost-user.

UML throughput varied substantially: single-guest sequential reads ranged from
**2,020–4,734 MiB/s** with host caching and **409–725 MiB/s** without it. Four-guest
cache-bypass reads ranged from **1,507–1,720 MiB/s** for UML and **1,781–1,791**
for KVM. The cause of the variation was not isolated. These are three trials on
a working desktop, a small working set and application queue depth 1; they do
not establish maximum SSD throughput or predict a full NixOS service workload.
The raw summary includes every throughput range and the measurements separate
guest-process CPU from backend CPU.

## Evidence

All **30 cohorts / 75 guest runs / 120 phases** completed. All **60 qcow2 images**
passed `qemu-img check`. The controller verified one KVM CPU per VM and zero CPUs
in each storage backend; every guest write phase completed a block flush.

[Raw CSV](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-block/measurements.csv),
[raw JSON with per-guest results](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-block/measurements.json),
[medians and ranges](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-block/summary.json),
[environment and store paths](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-block/metadata.json),
[validation summary](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-block/validation.json),
[separate traced flush validation](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28-block/flush-validation.json).

Implementation: [controller](pkgs/by-name/us/user-mode-linux/benchmark-block.py),
[workload](pkgs/by-name/us/user-mode-linux/benchmark-block.c).
QEMU documents the [shared-memory vhost-user frontend](https://www.qemu.org/docs/master/system/devices/virtio/vhost-user.html).
