"""CPU scaling and syscall cost; boot/warmup excluded, identical probe everywhere."""

import argparse
from contextlib import ExitStack
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

from benchmark_metrics import family, stop
from block_root import QMP, wait_path

CASES = [
    "native",
    "uml-ptrace",
    "uml-seccomp",
    "qemu-kvm-1",
    "qemu-kvm-8",
    "gvisor-systrap",
    "gvisor-ptrace",
    "gvisor-kvm",
]


def snapshot(root):
    result = {}
    for pid in family(root):
        for task in Path(f"/proc/{pid}/task").glob("*"):
            try:
                cpu = int((task / "schedstat").read_text().split()[0]) / 1e9
                switches = sum(
                    int(line.split()[1])
                    for line in (task / "status").read_text().splitlines()
                    if line.startswith(
                        ("voluntary_ctxt_switches:", "nonvoluntary_ctxt_switches:")
                    )
                )
                result[int(task.name)] = (cpu, switches)
            except (FileNotFoundError, ProcessLookupError):
                pass
    return result


class Console:
    def __init__(self, process, log):
        self.process, self.log = process, log
        self.buffer, self.messages = b"", {}

    def send(self, command):
        self.process.stdin.write((command + "\n").encode())
        self.process.stdin.flush()

    def wait(self, marker, sample=None):
        deadline = time.monotonic() + 120
        with selectors.DefaultSelector() as selector:
            selector.register(self.process.stdout, selectors.EVENT_READ)
            while marker not in self.messages:
                if time.monotonic() > deadline:
                    raise TimeoutError(marker)
                if sample:
                    sample()
                for _ in selector.select(0.05):
                    chunk = os.read(self.process.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError(
                            f"process exited waiting for {marker}; see {self.log.name}"
                        )
                    self.log.write(chunk)
                    self.log.flush()
                    self.buffer += chunk
                    while b"\n" in self.buffer:
                        line, self.buffer = self.buffer.split(b"\n", 1)
                        text = line.decode(errors="replace").strip()
                        if text.startswith("CPU_"):
                            key, _, value = text.partition(" ")
                            self.messages[key] = json.loads(value) if value else True
            return self.messages.pop(marker)


def run(args, case, trial, index):
    logdir = args.output / f"{index:02d}-{case}-trial{trial}"
    logdir.mkdir()
    with (
        tempfile.TemporaryDirectory(dir=args.runtime) as directory,
        ExitStack() as stack,
    ):
        work = Path(directory)
        qmp_socket = None
        if case == "native":
            command = [args.probe]
        elif case.startswith("gvisor-"):
            command = [
                args.runsc,
                f"--root={work}/runsc",
                "--rootless",
                "--ignore-cgroups",
                "--network=none",
                f"--platform={case.removeprefix('gvisor-')}",
                "do",
                "--quiet",
                args.probe,
            ]
        elif case.startswith("uml-"):
            command = [
                args.kernel,
                f"mem={args.memory_mib}M",
                f"initrd={args.initrd}",
                "con=null",
                "con0=fd:0,fd:1",
                "ssl=null",
                f"uml_dir={work}",
                "panic=-1",
                "quiet",
                f"seccomp={'off' if case == 'uml-ptrace' else 'on'}",
            ]
            if args.trace_uml:
                # Do not use -f: UML itself is the tracer of its guest processes.
                command = [
                    args.strace,
                    "-c",
                    "-e",
                    "trace=ptrace,wait4,futex",
                    "-o",
                    str(logdir / "strace.txt"),
                    *command,
                ]
        else:
            cpus = int(case.rsplit("-", 1)[1])
            qmp_socket = work / "qmp.sock"
            command = [
                args.qemu,
                "-machine",
                "q35,accel=kvm",
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
                str(cpus),
                "-m",
                str(args.memory_mib),
                "-kernel",
                args.native_kernel,
                "-initrd",
                args.initrd,
                "-append",
                "console=ttyS0 panic=-1 quiet",
                "-qmp",
                f"unix:{qmp_socket},server=on,wait=off",
            ]
        (logdir / "command.json").write_text(json.dumps(command, indent=2) + "\n")
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=os.environ | {"TMPDIR": str(work)},
            start_new_session=True,
        )
        stack.callback(stop, process)
        console = Console(
            process, stack.enter_context((logdir / "console.log").open("wb"))
        )
        if qmp_socket:
            wait_path(qmp_socket, process)
            sock = stack.enter_context(
                socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            )
            sock.connect(str(qmp_socket))
            qmp = QMP(sock)
            kvm = qmp.execute("query-kvm")
            vcpus = qmp.execute("query-cpus-fast")
            assert kvm["enabled"] and len(vcpus) == cpus
            (logdir / "qmp.json").write_text(
                json.dumps({"kvm": kvm, "cpus": vcpus}, indent=2) + "\n"
            )
        boot = console.wait("CPU_BOOT")
        if case.startswith("uml-"):
            assert boot["online_cpus"] == 1, boot
        if case.startswith("qemu-"):
            assert boot["online_cpus"] == cpus, boot
        jobs = [
            (w, n)
            for w in args.workloads
            for n in (args.threads if w == "compute" else args.syscall_threads)
        ]
        random.Random(42 + trial).shuffle(jobs)
        rows = []
        for workload, threads in jobs:
            console.send(f"PREP {workload} {threads} {args.seconds}")
            console.wait("CPU_READY")
            before = snapshot(process.pid)
            totals = dict(before)

            def sample():
                # Retain counters from short-lived runtime helper threads. The
                # workload threads persist; very short helpers may evade sampling.
                totals.update(snapshot(process.pid))

            start = time.monotonic()
            console.send("GO")
            result = console.wait("CPU_RESULT", sample)
            host_elapsed = time.monotonic() - start
            sample()
            cpu = sum(v[0] - before.get(tid, (0, 0))[0] for tid, v in totals.items())
            switches = sum(
                v[1] - before.get(tid, (0, 0))[1] for tid, v in totals.items()
            )
            assert result["workload"] == workload and result["threads"] == threads
            assert result["operations"] > 0 and result["seconds"] >= args.seconds
            row = {
                "case": case,
                "trial": trial,
                "traced": args.trace_uml,
                **boot,
                **result,
                "host_seconds": host_elapsed,
                "operations_per_second": result["operations"] / result["seconds"],
                "ns_per_operation": 1e9 * result["seconds"] / result["operations"],
                "host_cpu_seconds": cpu,
                "host_busy_cores": cpu / result["seconds"],
                "host_context_switches": switches,
                "host_switches_per_operation": switches / result["operations"],
                "application_busy_cores": result["application_cpu_seconds"]
                / result["seconds"],
            }
            rows.append(row)
            (logdir / f"{workload}-{threads}.json").write_text(
                json.dumps(row, indent=2) + "\n"
            )
            print(
                f"{case:15} trial={trial} {workload:7} threads={threads} "
                f"{row['operations_per_second'] / 1e6:8.3f} Mops/s "
                f"host CPU={row['host_busy_cores']:.2f} app CPU={row['application_busy_cores']:.2f}",
                flush=True,
            )
        console.send("QUIT")
        console.wait("CPU_SUCCESS")
        assert process.wait(timeout=30) == 0
        return rows


def main(args):
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.cpus:
        os.sched_setaffinity(0, {int(c) for c in args.cpus.split(",")})
    metadata = {
        "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": {k: v for k, v in platform.uname()._asdict().items() if k != "node"},
        "cpuinfo": Path("/proc/cpuinfo").read_text().split("\n\n")[0],
        "affinity": sorted(os.sched_getaffinity(0)),
        "host_mitigations": {
            p.name: p.read_text().strip()
            for p in Path("/sys/devices/system/cpu/vulnerabilities").glob("*")
        },
        "controller_seccomp": [
            line
            for line in Path("/proc/self/status").read_text().splitlines()
            if line.startswith("Seccomp")
        ],
        "arguments": {
            k: str(v) if isinstance(v, Path) else v for k, v in vars(args).items()
        },
        "qemu_version": subprocess.check_output(
            [args.qemu, "--version"], text=True
        ).splitlines()[0],
        "runsc_version": subprocess.check_output([args.runsc, "--version"], text=True),
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    jobs = [(case, trial) for trial in range(1, args.trials + 1) for case in args.cases]
    random.Random(42).shuffle(jobs)
    rows = []
    for index, (case, trial) in enumerate(jobs):
        rows.extend(run(args, case, trial, index))
        (args.output / "measurements.json").write_text(
            json.dumps(rows, indent=2) + "\n"
        )
    summary = []
    for case, workload, threads in sorted(
        {(r["case"], r["workload"], r["threads"]) for r in rows}
    ):
        selected = [
            r
            for r in rows
            if (r["case"], r["workload"], r["threads"]) == (case, workload, threads)
        ]
        summary.append(
            {
                "case": case,
                "workload": workload,
                "threads": threads,
                "trials": len(selected),
                **{
                    k: statistics.median(r[k] for r in selected)
                    for k in (
                        "operations_per_second",
                        "ns_per_operation",
                        "host_busy_cores",
                        "application_busy_cores",
                        "host_switches_per_operation",
                    )
                },
                "min_operations_per_second": min(
                    r["operations_per_second"] for r in selected
                ),
                "max_operations_per_second": max(
                    r["operations_per_second"] for r in selected
                ),
            }
        )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for flag in (
        "kernel",
        "native-kernel",
        "initrd",
        "probe",
        "qemu",
        "runsc",
        "strace",
    ):
        parser.add_argument(f"--{flag}", required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--cases", type=lambda s: s.split(","), default=CASES)
    parser.add_argument(
        "--workloads",
        type=lambda s: s.split(","),
        default=["compute", "gettid", "futex"],
    )
    parser.add_argument(
        "--threads", type=lambda s: [int(n) for n in s.split(",")], default=[1, 2, 4, 8]
    )
    parser.add_argument(
        "--syscall-threads", type=lambda s: [int(n) for n in s.split(",")], default=[1]
    )
    parser.add_argument("--seconds", type=float, default=2)
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--memory-mib", type=int, default=512)
    parser.add_argument("--cpus", help="Host CPU affinity, e.g. 0,1,2,3,4,5,6,7")
    parser.add_argument(
        "--trace-uml",
        action="store_true",
        help="Separate ptrace/wait4/futex validation; perturbs timing",
    )
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if set(args.cases) - set(CASES) or set(args.workloads) - {
        "compute",
        "gettid",
        "futex",
    }:
        parser.error("unknown case or workload")
    if (
        min(
            args.threads
            + args.syscall_threads
            + [args.seconds, args.trials, args.memory_mib]
        )
        <= 0
        or max(args.threads + args.syscall_threads) > 8
    ):
        parser.error("positive parameters required; worker threads must be 1..8")
    if args.trace_uml and any(not c.startswith("uml-") for c in args.cases):
        parser.error("--trace-uml requires only UML cases")
    main(args)
