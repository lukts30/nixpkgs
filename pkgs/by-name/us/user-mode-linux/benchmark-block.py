"""Compare UML UBD, UML vhost-user-blk and KVM with the same QEMU block backend."""

import argparse
from contextlib import ExitStack
import csv
import datetime
import json
import os
from pathlib import Path
import platform
import random
import selectors
import socket
import statistics
import subprocess
import tempfile
import time

from benchmark_metrics import cpu_snapshot, cpu_delta, memory, resident_mib, stop
from block_root import QMP, wait_path

MIB = 1024**2
PHASES = ["sequential-read", "random-read-4k", "sequential-write", "random-write-4k"]
CASES = [
    "uml-ubd-writeback",
    "uml-vhost-writeback",
    "kvm-vhost-writeback",
    "uml-vhost-none",
    "kvm-vhost-none",
]


class Console:
    def __init__(self, process, log):
        self.process, self.log = process, log
        self.buffer = b""
        self.ready = None
        self.results = {}
        self.success = False

    def read(self):
        chunk = os.read(self.process.stdout.fileno(), 65536)
        if not chunk:
            return False
        self.log.write(chunk)
        self.log.flush()
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            text = line.decode(errors="replace").strip()
            if text.startswith("BLOCK_READY "):
                self.ready = json.loads(text.removeprefix("BLOCK_READY "))
            elif text.startswith("BLOCK_RESULT "):
                result = json.loads(text.removeprefix("BLOCK_RESULT "))
                self.results[result["phase"]] = result
            elif text == "BLOCK_BENCHMARK_SUCCESS":
                self.success = True
        return True

    def go(self, phase):
        self.process.stdin.write(f"GO {phase}\n".encode())
        self.process.stdin.flush()


def poll(selector, processes, until, timeout=120, sample=None):
    deadline = time.monotonic() + timeout
    next_sample = time.monotonic() + 0.25
    while not until():
        for key, _ in selector.select(0.01):
            if not key.data.read():
                selector.unregister(key.fileobj)
        if until():
            break
        if any(process.poll() is not None for process in processes):
            raise RuntimeError("guest/backend exited before completing benchmark")
        if time.monotonic() > deadline:
            raise TimeoutError("guest console barrier")
        if sample and time.monotonic() >= next_sample:
            sample()
            next_sample = time.monotonic() + 0.25


def cohort(args, seed, case, count, trial, index):
    logdir = args.output / f"{index:02d}-{case}-{count}-trial{trial}"
    logdir.mkdir()
    with tempfile.TemporaryDirectory(prefix="uml-block-data-") as directory:
        data = Path(directory)
        with tempfile.TemporaryDirectory(dir=args.runtime) as runtime_dir:
            runtime = Path(runtime_dir)
            with ExitStack() as stack:
                selector = stack.enter_context(selectors.DefaultSelector())
                guests, backends, consoles, disks = [], [], [], []

                def start(command, name, console=False, env=None):
                    log = stack.enter_context((logdir / name).open("wb"))
                    if args.trace_flush and not console:
                        command = [
                            args.strace,
                            "-ff",
                            "-e",
                            "trace=fsync,fdatasync",
                            "-o",
                            str(logdir / f"{name}.trace"),
                            *command,
                        ]
                    process = subprocess.Popen(
                        command,
                        stdin=subprocess.PIPE if console else subprocess.DEVNULL,
                        stdout=subprocess.PIPE if console else log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        env=env,
                        bufsize=0,
                    )
                    stack.callback(stop, process)
                    if console:
                        channel = Console(process, log)
                        selector.register(process.stdout, selectors.EVENT_READ, channel)
                        consoles.append(channel)
                    return process

                def connect_qmp(path, process):
                    wait_path(path, process)
                    connection = stack.enter_context(socket.socket(socket.AF_UNIX))
                    connection.settimeout(15)
                    connection.connect(str(path))
                    qmp = QMP(connection)
                    stack.callback(qmp.file.close)
                    return qmp

                for guest in range(count):
                    ubd = case == "uml-ubd-writeback"
                    kvm = case.startswith("kvm-")
                    direct = case.endswith("-none")
                    disk = data / f"disk-{guest}.{'raw' if ubd else 'qcow2'}"
                    subprocess.run(
                        [
                            "qemu-img",
                            "convert",
                            "-f",
                            "raw",
                            "-O",
                            "raw" if ubd else "qcow2",
                            str(seed),
                            str(disk),
                        ],
                        check=True,
                    )
                    with disk.open("rb") as file:
                        os.fsync(file.fileno())
                        if direct:
                            os.posix_fadvise(
                                file.fileno(), 0, 0, os.POSIX_FADV_DONTNEED
                            )
                        else:
                            while file.read(MIB):
                                pass
                    disks.append(disk)
                    work = runtime / str(guest)
                    work.mkdir()
                    devices = [f"ubd0={disk}"]
                    if not ubd:
                        qmp_socket, block_socket = (
                            work / "storage.qmp",
                            work / "block.sock",
                        )
                        backend = start(
                            [
                                args.qemu,
                                "-machine",
                                "none",
                                "-nodefaults",
                                "-display",
                                "none",
                                "-monitor",
                                "none",
                                "-serial",
                                "none",
                                "-S",
                                "-qmp",
                                f"unix:{qmp_socket},server=on,wait=off",
                                "-blockdev",
                                json.dumps(
                                    {
                                        "driver": "file",
                                        "node-name": "file",
                                        "filename": str(disk),
                                        "aio": "threads",
                                        "cache": {"direct": direct, "no-flush": False},
                                    }
                                ),
                                "-blockdev",
                                "driver=qcow2,node-name=disk,file=file,cache.no-flush=off",
                            ],
                            f"backend-{guest}.log",
                        )
                        backends.append(backend)
                        qmp = connect_qmp(qmp_socket, backend)
                        assert qmp.execute("query-cpus-fast") == []
                        qmp.execute(
                            "block-export-add",
                            {
                                "type": "vhost-user-blk",
                                "id": "disk",
                                "node-name": "disk",
                                "writable": True,
                                "addr": {"type": "unix", "path": str(block_socket)},
                            },
                        )
                        (logdir / f"backend-{guest}.json").write_text(
                            json.dumps(
                                {
                                    "cpus": [],
                                    "exports": qmp.execute("query-block-exports"),
                                    "block_nodes": qmp.execute(
                                        "query-named-block-nodes"
                                    ),
                                },
                                indent=2,
                            )
                            + "\n"
                        )
                        wait_path(block_socket, backend)
                        devices = [f"virtio_uml.device={block_socket}:2"]
                    cmdline = [
                        "panic=-1",
                        "quiet",
                        f"bench_seconds={args.seconds}",
                        f"bench_device={'/dev/ubda' if ubd else '/dev/vda'}",
                    ]
                    if kvm:
                        qmp_socket = work / "guest.qmp"
                        command = [
                            args.qemu,
                            "-machine",
                            "q35,accel=kvm,memory-backend=ram",
                            "-nodefaults",
                            "-display",
                            "none",
                            "-monitor",
                            "none",
                            "-serial",
                            "stdio",
                            "-cpu",
                            "host",
                            "-smp",
                            "1",
                            "-m",
                            str(args.memory_mib),
                            "-object",
                            f"memory-backend-memfd,id=ram,size={args.memory_mib}M,share=on",
                            "-kernel",
                            args.native_kernel,
                            "-initrd",
                            args.initrd,
                            "-append",
                            " ".join(["console=ttyS0", *cmdline]),
                            "-chardev",
                            f"socket,id=block,path={block_socket}",
                            "-device",
                            "vhost-user-blk-pci,chardev=block,num-queues=1",
                            "-qmp",
                            f"unix:{qmp_socket},server=on,wait=off",
                        ]
                    else:
                        command = [
                            args.kernel,
                            f"mem={args.memory_mib}M",
                            f"initrd={args.initrd}",
                            "con=null",
                            "con0=fd:0,fd:1",
                            "ssl=null",
                            f"uml_dir={work}",
                            *devices,
                            *cmdline,
                        ]
                    process = start(
                        command,
                        f"guest-{guest}.log",
                        console=True,
                        env=os.environ | {"TMPDIR": str(work)},
                    )
                    guests.append(process)
                    if kvm:
                        qmp = connect_qmp(qmp_socket, process)
                        kvm_info = qmp.execute("query-kvm")
                        cpus = qmp.execute("query-cpus-fast")
                        assert kvm_info["enabled"] and len(cpus) == 1
                        (logdir / f"guest-{guest}.json").write_text(
                            json.dumps({"kvm": kvm_info, "cpus": cpus}, indent=2) + "\n"
                        )
                processes = guests + backends
                guest_ids, backend_ids = (
                    [p.pid for p in guests],
                    [p.pid for p in backends],
                )
                all_ids = guest_ids + backend_ids
                poll(selector, processes, lambda: all(c.ready for c in consoles))
                assert all(c.ready["size"] == args.size_mib * MIB for c in consoles)
                baseline = memory(all_ids)
                baseline["guest_cached_mib"] = (
                    sum(c.ready["cached_kib"] for c in consoles) / 1024
                )
                baseline["host_file_cache_mib"] = sum(resident_mib(d) for d in disks)
                rows = []

                def backend_flushes():
                    return [
                        sum(
                            line.startswith(("fsync(", "fdatasync("))
                            and line.endswith("= 0")
                            for path in logdir.glob(f"backend-{guest}.log.trace.*")
                            for line in path.read_text().splitlines()
                        )
                        for guest in range(count)
                        if backends and args.trace_flush
                    ]

                for phase, name in enumerate(PHASES):
                    before_flushes = backend_flushes()
                    before_guest, before_backend = (
                        cpu_snapshot(guest_ids),
                        cpu_snapshot(backend_ids),
                    )
                    samples = []
                    start_time = time.monotonic()
                    for console in consoles:
                        console.go(phase)
                    poll(
                        selector,
                        processes,
                        lambda: all(phase in c.results for c in consoles),
                        timeout=max(120, args.seconds * 3),
                        sample=lambda: samples.append(memory(all_ids)),
                    )
                    elapsed = time.monotonic() - start_time
                    guest_cpu = cpu_delta(before_guest, cpu_snapshot(guest_ids))
                    backend_cpu = cpu_delta(before_backend, cpu_snapshot(backend_ids))
                    final_memory = memory(all_ids)
                    samples.append(final_memory)
                    results = [c.results[phase] for c in consoles]
                    flush_operations = [
                        after - before
                        for before, after in zip(
                            before_flushes, backend_flushes(), strict=True
                        )
                    ]
                    if phase >= 2:
                        assert all(r["flushes"] > 0 for r in results), results
                        assert all(n > 0 for n in flush_operations), flush_operations
                    transferred = sum(r["bytes"] for r in results)
                    row = {
                        "case": case,
                        "guests": count,
                        "trial": trial,
                        "phase": name,
                        "host_seconds": elapsed,
                        "bytes": transferred,
                        "mib_per_second": transferred / MIB / elapsed,
                        "iops": sum(r["operations"] for r in results) / elapsed,
                        "guest_cpu_seconds": guest_cpu,
                        "backend_cpu_seconds": backend_cpu,
                        "cpu_seconds_per_gib": (guest_cpu + backend_cpu)
                        / (transferred / 1024**3),
                        "busy_cores": (guest_cpu + backend_cpu) / elapsed,
                        "end_pss_mib": final_memory["pss_mib"],
                        "peak_sampled_pss_mib": max(s["pss_mib"] for s in samples),
                        "swap_pss_mib": max(s["swappss_mib"] for s in samples),
                        "guest_cached_mib": sum(r["cached_kib"] for r in results)
                        / 1024,
                        "host_file_cache_mib": sum(resident_mib(d) for d in disks),
                        "baseline": baseline,
                        "guest_results": results,
                        "traced_backend_flush_operations": flush_operations,
                    }
                    rows.append(row)
                    (logdir / f"{name}.json").write_text(
                        json.dumps(row, indent=2) + "\n"
                    )
                    print(
                        f"{case:20} guests={count} trial={trial} {name:18} {row['mib_per_second']:8.1f} MiB/s {row['cpu_seconds_per_gib']:7.2f} CPU-s/GiB PSS={row['end_pss_mib']:.1f} MiB",
                        flush=True,
                    )
                for console in consoles:
                    console.go(4)
                poll(
                    selector,
                    processes,
                    lambda: all(c.success for c in consoles),
                    timeout=30,
                )
                for process in guests:
                    assert process.wait(timeout=30) == 0
            for disk in disks:
                if disk.suffix == ".qcow2":
                    with (logdir / f"{disk.stem}-check.log").open("w") as log:
                        subprocess.run(
                            ["qemu-img", "check", str(disk)],
                            stdout=log,
                            stderr=subprocess.STDOUT,
                            check=True,
                        )
    return rows


def summarize(output, rows):
    columns = [key for key in rows[0] if key not in {"baseline", "guest_results"}]
    with (output / "measurements.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows({key: row[key] for key in columns} for row in rows)
    summary = []
    metrics = [
        "mib_per_second",
        "iops",
        "cpu_seconds_per_gib",
        "busy_cores",
        "end_pss_mib",
        "host_file_cache_mib",
        "guest_cached_mib",
    ]
    for count in sorted({r["guests"] for r in rows}):
        for case in CASES:
            for phase in PHASES:
                selected = [
                    r
                    for r in rows
                    if (r["guests"], r["case"], r["phase"]) == (count, case, phase)
                ]
                if not selected:
                    continue
                summary.append(
                    {
                        "guests": count,
                        "case": case,
                        "phase": phase,
                        **{
                            m: statistics.median(r[m] for r in selected)
                            for m in metrics
                        },
                        "throughput_min": min(r["mib_per_second"] for r in selected),
                        "throughput_max": max(r["mib_per_second"] for r in selected),
                    }
                )
    (output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


def main(args):
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.cpus:
        os.sched_setaffinity(0, {int(c) for c in args.cpus.split(",")})
    if any(c.startswith("kvm") for c in args.cases):
        with open("/dev/kvm", "rb+"):
            pass
    metadata = {
        "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": {k: v for k, v in platform.uname()._asdict().items() if k != "node"},
        "cpuinfo": Path("/proc/cpuinfo").read_text().split("\n\n")[0],
        "affinity": sorted(os.sched_getaffinity(0)),
        "arguments": {
            k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
        },
        "host_meminfo_before": Path("/proc/meminfo").read_text(),
        "qemu_version": subprocess.check_output(
            [args.qemu, "--version"], text=True
        ).splitlines()[0],
        "data_filesystem": subprocess.check_output(
            ["stat", "-f", "-c", "%T", tempfile.gettempdir()], text=True
        ).strip(),
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    with tempfile.TemporaryDirectory(prefix="uml-block-seed-") as directory:
        seed = Path(directory) / "seed.raw"
        with seed.open("wb") as file:
            block = os.urandom(MIB)
            for _ in range(args.size_mib):
                file.write(block)
            file.flush()
            os.fsync(file.fileno())
        cases = [
            (case, count, trial)
            for trial in range(1, args.trials + 1)
            for count in args.guests
            for case in args.cases
        ]
        random.Random(42).shuffle(cases)
        rows = []
        for index, (case, count, trial) in enumerate(cases):
            rows.extend(cohort(args, seed, case, count, trial, index))
            (args.output / "measurements.json").write_text(
                json.dumps(rows, indent=2) + "\n"
            )
    summarize(args.output, rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for flag in (
        "kernel",
        "native-kernel",
        "kernel-version",
        "initrd",
        "qemu",
        "strace",
    ):
        parser.add_argument(f"--{flag}", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--cases", type=lambda s: s.split(","), default=CASES)
    parser.add_argument(
        "--guests", type=lambda s: [int(n) for n in s.split(",")], default=[1, 4]
    )
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--seconds", type=float, default=5)
    parser.add_argument("--size-mib", type=int, default=128)
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--cpus")
    parser.add_argument(
        "--trace-flush",
        action="store_true",
        help="Trace backend fsync/fdatasync for validation; do not use for performance results",
    )
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if set(args.cases) - set(CASES):
        parser.error("unknown benchmark case")
    if (
        min(args.guests + [args.trials, args.size_mib, args.memory_mib, args.seconds])
        <= 0
    ):
        parser.error("counts, sizes and duration must be positive")
    main(args)
