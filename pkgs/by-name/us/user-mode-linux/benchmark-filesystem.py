"""Repeatable multi-UML filesystem microbenchmark; raw measurements remain auditable."""

import argparse
from contextlib import ExitStack
import csv
import ctypes
import datetime
import json
import mmap
import os
from pathlib import Path
import platform
import random
import signal
import statistics
import subprocess
import tempfile
import time

MIB = 1024 * 1024
PHASES = ["first-read", "repeat-read", "random-read-4k", "write-fdatasync"]


def family(root):
    """Include children created by every thread, including UML's traced processes."""
    seen, pending = set(), [root]
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        for path in Path(f"/proc/{pid}/task").glob("*/children"):
            try:
                pending.extend(int(child) for child in path.read_text().split())
            except FileNotFoundError:
                pass
    return seen


def cpu_snapshot(roots):
    times = {}
    pids = set().union(*(family(root) for root in roots))
    for pid in pids:
        for path in Path(f"/proc/{pid}/task").glob("*/schedstat"):
            # Scheduler runtime has nanosecond resolution; /proc/pid/stat's
            # 10 ms ticks are too coarse for the short first-read/write phases.
            times[int(path.parent.name)] = int(path.read_text().split()[0])
    return times


def cpu_delta(before, after):
    if before.keys() - after.keys():
        raise RuntimeError("CPU-accounted thread exited during a phase")
    return sum(value - before.get(tid, 0) for tid, value in after.items()) / 1e9


def memory(roots):
    totals = {"Pss": 0, "Rss": 0, "SwapPss": 0}
    pids = set().union(*(family(root) for root in roots))
    for pid in pids:
        try:
            for line in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
                key, _, value = line.partition(":")
                if key in totals:
                    totals[key] += int(value.split()[0])
        except (FileNotFoundError, ProcessLookupError):
            pass
    return {key.lower() + "_mib": value / 1024 for key, value in totals.items()}


def resident_mib(path):
    """mincore inspects file-cache residency without faulting file pages in."""
    length = path.stat().st_size
    if not length:
        return 0
    with path.open("rb") as file:
        with mmap.mmap(
            file.fileno(),
            length,
            flags=mmap.MAP_PRIVATE,
            prot=mmap.PROT_READ | mmap.PROT_WRITE,
        ) as mapping:
            address = ctypes.addressof(ctypes.c_char.from_buffer(mapping))
            pages = (length + mmap.PAGESIZE - 1) // mmap.PAGESIZE
            vector = (ctypes.c_ubyte * pages)()
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.mincore(ctypes.c_void_p(address), ctypes.c_size_t(length), vector):
                raise OSError(ctypes.get_errno(), "mincore")
            return sum(value & 1 for value in vector) * mmap.PAGESIZE / MIB


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    # UML tracing helpers must also be gone after a failed boot.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


def wait_for(condition, processes, timeout=120):
    deadline = time.monotonic() + timeout
    while not condition():
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(
                    f"process exited: {process.args} ({process.returncode})"
                )
        if time.monotonic() > deadline:
            raise TimeoutError("guest barrier")
        time.sleep(0.01)


def cohort(args, data, mode, guests, trial, index):
    logdir = args.output / f"{index:02d}-{mode}-{guests}-trial{trial}"
    logdir.mkdir()
    with tempfile.TemporaryDirectory(dir=args.runtime, prefix="guests-") as directory:
        runtime = Path(directory)
        with ExitStack() as stack:
            kernels, daemons, controls = [], [], []

            def start(command, log, env=None):
                file = stack.enter_context((logdir / log).open("w"))
                process = subprocess.Popen(
                    command,
                    stdout=file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
                stack.callback(stop, process)
                return process

            # Warm only the host's source-file cache. Each cohort has fresh guest
            # kernels and fresh mounts; no global cache-dropping is involved.
            with (data / "input").open("rb") as file:
                while file.read(MIB):
                    pass
            for guest in range(guests):
                work = runtime / str(guest)
                work.mkdir()
                control = work / "control"
                control.mkdir()
                controls.append(control)
                devices = []
                if mode != "hostfs":
                    transport = work / "fs.sock"
                    daemon = start(
                        [
                            args.virtiofsd,
                            f"--socket-path={transport}",
                            f"--shared-dir={data}",
                            "--tag=benchmark",
                            "--sandbox=none",
                            f"--cache={mode}",
                            "--inode-file-handles=never",
                            "--translate-uid=map:0:0:1",
                            "--translate-gid=map:0:0:1",
                        ],
                        f"daemon-{guest}.log",
                    )
                    daemons.append(daemon)
                    wait_for(lambda: transport.exists(), [daemon])
                    devices = [f"virtio_uml.device={transport}:26"]
                kernels.append(
                    start(
                        [
                            args.kernel,
                            f"mem={args.memory_mib}M",
                            f"initrd={args.initrd}",
                            "con=null",
                            "con0=fd:0,fd:1",
                            "ssl=null",
                            f"uml_dir={work}",
                            f"bench_control={control}",
                            f"bench_data={data}",
                            f"bench_mode={mode}",
                            f"bench_id={guest}",
                            f"bench_seconds={args.seconds}",
                            *devices,
                        ],
                        f"guest-{guest}.log",
                        os.environ | {"TMPDIR": str(work)},
                    )
                )
            processes = kernels + daemons
            kernel_ids = [process.pid for process in kernels]
            daemon_ids = [process.pid for process in daemons]
            all_ids = kernel_ids + daemon_ids
            wait_for(
                lambda: all((path / "phase--1.json").exists() for path in controls),
                processes,
            )
            baseline = memory(all_ids)
            baseline["guest_cached_mib"] = (
                sum(
                    json.loads((p / "phase--1.json").read_text())["cached_kib"]
                    for p in controls
                )
                / 1024
            )
            baseline["host_file_cache_mib"] = resident_mib(data / "input")
            rows = []
            for phase, name in enumerate(PHASES):
                before_kernel = cpu_snapshot(kernel_ids)
                before_daemon = cpu_snapshot(daemon_ids)
                start_time = time.monotonic()
                for path in controls:
                    (path / f"go-{phase}").touch()
                samples = []
                deadline = start_time + max(120, args.seconds * 3)
                next_sample = start_time + 0.25
                while not all(
                    (path / f"phase-{phase}.json").exists() for path in controls
                ):
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError("benchmark process exited")
                    if time.monotonic() > deadline:
                        raise TimeoutError(name)
                    if time.monotonic() >= next_sample:
                        samples.append(memory(all_ids))
                        next_sample = time.monotonic() + 0.25
                    time.sleep(0.01)
                elapsed = time.monotonic() - start_time
                kernel_cpu = cpu_delta(before_kernel, cpu_snapshot(kernel_ids))
                daemon_cpu = cpu_delta(before_daemon, cpu_snapshot(daemon_ids))
                final_memory = memory(all_ids)
                samples.append(final_memory)
                results = [
                    json.loads((path / f"phase-{phase}.json").read_text())
                    for path in controls
                ]
                total_bytes = sum(result["bytes"] for result in results)
                host_cache = sum(
                    resident_mib(file) for file in data.iterdir() if file.is_file()
                )
                row = {
                    "mode": mode,
                    "guests": guests,
                    "trial": trial,
                    "phase": name,
                    "host_seconds": elapsed,
                    "bytes": total_bytes,
                    "mib_per_second": total_bytes / MIB / elapsed,
                    "iops": sum(result["operations"] for result in results) / elapsed,
                    "uml_cpu_seconds": kernel_cpu,
                    "daemon_cpu_seconds": daemon_cpu,
                    "cpu_seconds_per_gib": (kernel_cpu + daemon_cpu)
                    / (total_bytes / 1024**3),
                    "busy_cores": (kernel_cpu + daemon_cpu) / elapsed,
                    "end_pss_mib": final_memory["pss_mib"],
                    "peak_sampled_pss_mib": max(
                        sample["pss_mib"] for sample in samples
                    ),
                    "pss_delta_mib": final_memory["pss_mib"] - baseline["pss_mib"],
                    "swap_pss_mib": max(sample["swappss_mib"] for sample in samples),
                    "guest_cached_mib": sum(result["cached_kib"] for result in results)
                    / 1024,
                    "host_file_cache_mib": host_cache,
                    "baseline": baseline,
                    "guest_results": results,
                }
                rows.append(row)
                (logdir / f"{name}.json").write_text(json.dumps(row, indent=2) + "\n")
                print(
                    f"{mode:6} guests={guests} trial={trial} {name:15} "
                    f"{row['mib_per_second']:8.1f} MiB/s {row['cpu_seconds_per_gib']:6.2f} CPU-s/GiB "
                    f"PSS={row['end_pss_mib']:.1f} MiB",
                    flush=True,
                )
            for path in controls:
                (path / "go-4").touch()
            for process in kernels:
                assert process.wait(timeout=30) == 0
    for path in data.glob("write-*"):
        path.unlink()
    return rows


def write_summary(output, rows):
    columns = [key for key in rows[0] if key not in ("baseline", "guest_results")]
    with (output / "measurements.csv").open("w") as file:
        writer = csv.DictWriter(file, fieldnames=columns)
        writer.writeheader()
        writer.writerows({key: row[key] for key in columns} for row in rows)
    metrics = [
        "mib_per_second",
        "cpu_seconds_per_gib",
        "busy_cores",
        "end_pss_mib",
        "peak_sampled_pss_mib",
        "pss_delta_mib",
        "guest_cached_mib",
        "host_file_cache_mib",
    ]
    summary = []
    for guests in sorted({row["guests"] for row in rows}):
        for mode in ("hostfs", "auto", "never"):
            for phase in PHASES:
                selected = [
                    row
                    for row in rows
                    if (row["guests"], row["mode"], row["phase"])
                    == (guests, mode, phase)
                ]
                summary.append(
                    {
                        "guests": guests,
                        "mode": mode,
                        "phase": phase,
                        **{
                            metric: statistics.median(row[metric] for row in selected)
                            for metric in metrics
                        },
                        "throughput_min": min(
                            row["mib_per_second"] for row in selected
                        ),
                        "throughput_max": max(
                            row["mib_per_second"] for row in selected
                        ),
                    }
                )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def main(args):
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.cpus:
        os.sched_setaffinity(0, {int(cpu) for cpu in args.cpus.split(",")})
    metadata = {
        "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": {
            key: value
            for key, value in platform.uname()._asdict().items()
            if key != "node"
        },
        "cpuinfo": Path("/proc/cpuinfo").read_text().split("\n\n")[0],
        "affinity": sorted(os.sched_getaffinity(0)),
        "arguments": {
            key: str(value) if isinstance(value, Path) else value
            for key, value in vars(args).items()
        },
        "host_meminfo_before": Path("/proc/meminfo").read_text(),
        "virtiofsd_version": subprocess.check_output(
            [args.virtiofsd, "--version"], text=True
        ).strip(),
        "data_filesystem": subprocess.check_output(
            ["stat", "-f", "-c", "%T", tempfile.gettempdir()], text=True
        ).strip(),
        "ram_filesystem": subprocess.check_output(
            ["stat", "-f", "-c", "%T", str(args.runtime)], text=True
        ).strip(),
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    rows = []
    with tempfile.TemporaryDirectory(prefix="uml-benchmark-data-") as directory:
        data = Path(directory)
        with (data / "input").open("wb") as file:
            block = os.urandom(MIB)
            for _ in range(args.size_mib):
                file.write(block)
            file.flush()
            os.fsync(file.fileno())
        cases = [
            (mode, guests, trial)
            for trial in range(1, args.trials + 1)
            for guests in args.guests
            for mode in ("hostfs", "auto", "never")
        ]
        random.Random(42).shuffle(cases)
        for index, (mode, guests, trial) in enumerate(cases):
            rows.extend(cohort(args, data, mode, guests, trial, index))
            (args.output / "measurements.json").write_text(
                json.dumps(rows, indent=2) + "\n"
            )
    write_summary(args.output, rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for flag in ("kernel", "initrd", "virtiofsd"):
        parser.add_argument(f"--{flag}", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument(
        "--guests",
        type=lambda text: [int(value) for value in text.split(",")],
        default=[1, 4],
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--size-mib", type=int, default=128)
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--cpus")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if (
        min(args.guests + [args.trials, args.size_mib, args.memory_mib, args.seconds])
        <= 0
    ):
        parser.error("counts, sizes and duration must be positive")
    main(args)
