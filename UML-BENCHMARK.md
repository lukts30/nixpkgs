# UML filesystem benchmark — 2026-10-28

Hostfs and virtio-fs `cache=auto` were essentially tied on repeated reads in
this run. `cache=never` reduced memory substantially but made repeated and small
random reads slower and more CPU-intensive. It was faster and used less CPU for
large writes followed by `fdatasync`. There is no overall winner for every workload.

## Rerun

```sh
nix-build -A user-mode-linux.benchmarks.filesystems -o result-uml-fs-benchmark
./result-uml-fs-benchmark/bin/uml-filesystem-benchmark \
  --guests 1,4 --trials 3 --seconds 5 --size-mib 128 --memory-mib 512 \
  --cpus 0,1,2,3,4,5,6,7 /tmp/uml-fs-results
```

Choose a new output directory for each run. Omit `--cpus` on other machines or
select suitable CPUs. Requires Linux with unprivileged user/mount namespaces;
no root or KVM. The runner mounts a private 6 GiB tmpfs for guest RAM, cleans up
its temporary files and processes, and leaves measurements plus boot/daemon logs.
It never drops the host's global page cache.

## Method

- AMD Ryzen 7 5800X, 32 GB RAM; host Linux 6.18.57, UML 6.18.59, virtiofsd 1.14.0.
  Affinity was CPUs 0–7, one thread from each of eight physical cores. CPUs were
  not reserved and frequency scaling remained enabled; this was a working desktop.
- One or four concurrent, single-CPU UML guests, 512 MiB each; one virtiofsd per
  virtio-fs guest. A minimal initramfs runs the C workload. This does not benchmark
  full NixOS startup, metadata operations, mmap, DAX, or application latency.
- All guests read the **same 128 MiB file**. The host cache is warmed before each
  cohort. Each cohort boots fresh guests; configuration order is shuffled with
  seed 42. Three repetitions per guest-count/filesystem combination.
- First read: one sequential pass into a fresh guest cache. Repeated read:
  sequential reads for five seconds. Random read: uniform 4 KiB reads for five
  seconds. Reads use `pread`, one request at a time per guest. Sequential requests
  are 1 MiB. The input FD stays open across read phases, so `auto` close-to-open
  invalidation is not included. Each guest then writes a private 128 MiB file with
  1 MiB `write` calls and `fdatasync` before closing.
- Data files reside on Btrfs (`compress=zstd:1`) on a Lexar NM790 NVMe SSD. The
  corpus repeats a random 1 MiB block, avoiding zero-filled/compressible data.
  The read results measure cached filesystem paths, **not cold storage throughput**.
- Throughput is aggregate bytes divided by host wall time from barrier release
  until every guest reports completion. Five-second loops run in 64-operation
  batches. The 5 ms guest / 10 ms host barrier polling and memory sampling add
  overhead, particularly to the short first-read/write phases.
- CPU is summed per-thread scheduler runtime for UML, its host helper processes
  and virtiofsd, divided by GiB transferred. Threads must survive the measured
  phase. It includes their syscall execution, but excludes the measurement
  process, unrelated tasks and asynchronous host kernel workers.
- Memory is summed process **PSS**, not summed RSS, so mappings of shared guest RAM
  are apportioned rather than counted again for each process. Samples are taken
  about every 250 ms and after each phase; raw results include sampled peaks.
  Guest `/proc/meminfo` and host file residency via `mincore` are recorded separately.
  PSS excludes unmapped host file cache and kernel bookkeeping. All measured
  process SwapPss values were zero.

## Throughput

Median of three runs, **aggregate MiB/s**; higher is better.

| Guests | Filesystem | First read | Repeated read | Random 4 KiB read | Write + fdatasync |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | hostfs | 1,052 | 7,634 | 271 | 389 |
| 1 | virtio-fs auto | 1,260 | 7,574 | 267 | 363 |
| 1 | virtio-fs never | 1,806 | 930 | 31 | 601 |
| 4 | hostfs | 3,580 | 21,835 | 1,072 | 867 |
| 4 | virtio-fs auto | 627 | 21,825 | 1,072 | 841 |
| 4 | virtio-fs never | 7,168 | 5,621 | 146 | 1,125 |

## CPU cost

Median **CPU-seconds per GiB**, summed across UML and its backend; lower is better.
Raw measurements also contain UML/backend CPU separately and average busy cores.

| Guests | Filesystem | First read | Repeated read | Random 4 KiB read | Write + fdatasync |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | hostfs | 0.89 | 0.13 | 3.55 | 2.24 |
| 1 | virtio-fs auto | 1.05 | 0.13 | 3.57 | 2.41 |
| 1 | virtio-fs never | 0.52 | 0.51 | 10.76 | 1.35 |
| 4 | hostfs | 1.00 | 0.18 | 3.58 | 2.57 |
| 4 | virtio-fs auto | 1.19 | 0.18 | 3.60 | 2.39 |
| 4 | virtio-fs never | 0.51 | 0.53 | 11.14 | 1.37 |

## Memory

Median MiB across all guests and backends. “Guest cache added” is the increase
in guest `Cached` after the read phases. PSS includes the runtime as well as data.

| Guests | Filesystem | Baseline PSS | PSS after reads | Guest cache added | PSS after writes |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | hostfs | 75.3 | 191.4 | 128.0 | 319.7 |
| 1 | virtio-fs auto | 78.1 | 194.2 | 128.0 | 322.5 |
| 1 | virtio-fs never | 78.1 | 78.1 | 0.0 | 78.1 |
| 4 | hostfs | 282.3 | 746.9 | 512.0 | 1,260.0 |
| 4 | virtio-fs auto | 286.5 | 751.1 | 512.0 | 1,264.4 |
| 4 | virtio-fs never | 286.5 | 286.7 | 0.0 | 286.7 |

The host retained **128 MiB of input file cache** in every configuration. Four
cached guests added another **512 MiB inside guest caches**: five cached copies
of the same file in total. `never` added no guest file-data cache. Including the
tracked host input cache, the four-guest read footprint was approximately
**875 MiB hostfs, 879 MiB auto, and 415 MiB never**. This sum still excludes host
kernel bookkeeping and other unmapped host caches; it is not total machine RAM.

After writes, tracked host file cache was 256 MiB with one guest and 640 MiB with
four, for every mode. Cached guests also retained their private output data.
Guest cache growth need not equal PSS growth: guest allocations can reuse RAM
pages already resident after boot.

## Interpretation and limits

With four guests, `auto` and hostfs differed by less than 1% on repeated-read
throughput and CPU cost. `never` delivered about **3.9× less sequential** and
**7.4× less random-read throughput**, costing about **2.9× and 3.1× more CPU per
GiB**, respectively. Its tracked read footprint was about **53% smaller**.
For the write-and-sync workload, `never` was about **1.3× faster than hostfs**
and cost about **47% less attributed CPU per GiB**.

First reads behaved differently: four-guest `auto` was much slower than hostfs
here, whereas `never` was faster. The cause was not isolated. Single-guest
`never` first-read runs ranged from 153 to 1,809 MiB/s; its repeated reads ranged
from 803 to 1,756 MiB/s. Four-guest repeated reads ranged from 21,298–22,037 MiB/s
for hostfs, 21,401–22,302 for `auto`, and 4,490–6,337 for `never`. Write results
also varied. Three trials on a working desktop do not establish universal
performance rankings, especially for the short phases.

For this runner, hostfs remains a reasonable default. `auto` provides similar
cached-read behavior with virtio-fs features. `never` is worth considering for
streaming data or memory-constrained guests, but these measurements do not cover
executable mappings; enabling `--allow-mmap` changes its caching/consistency scope.

## Evidence

[Raw CSV](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28/measurements.csv),
[raw JSON including each guest](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28/measurements.json),
[medians and ranges](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28/summary.json),
[environment and exact store paths](pkgs/by-name/us/user-mode-linux/benchmark-results/2026-10-28/metadata.json).

Implementation: [host controller](pkgs/by-name/us/user-mode-linux/benchmark-filesystem.py),
[guest workload](pkgs/by-name/us/user-mode-linux/benchmark-filesystem.c).
Accounting references: [scheduler runtime](https://docs.kernel.org/scheduler/sched-stats.html#proc-pid-schedstat),
[PSS](https://docs.kernel.org/filesystems/proc.html),
[virtiofsd cache policies](https://virtio-fs.gitlab.io/virtiofsd/doc/virtiofsd/passthrough/enum.CachePolicy.html).
