"""Compare complete, prebuilt NixOS test drivers without timing Nix builds."""

import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import random
import resource
import statistics
import subprocess
import tempfile
import time


def run(args, drivers, case, trial, index):
    output = args.output / f"{index:02d}-{case}-trial{trial}"
    output.mkdir()
    with tempfile.TemporaryDirectory(dir=args.runtime) as directory:
        runtime = Path(directory)
        state = runtime / "state"
        state.mkdir(mode=0o700)
        command = [drivers[case], "--no-interactive", "-o", str(output)]
        before = resource.getrusage(resource.RUSAGE_CHILDREN)
        with (output / "driver.log").open("w") as log:
            start = time.monotonic()
            subprocess.run(
                command,
                env=os.environ
                | {
                    "TMPDIR": str(runtime),
                    "XDG_RUNTIME_DIR": str(state),
                    "LOGFILE": str(output / "log.xml"),
                },
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
            elapsed = time.monotonic() - start
        after = resource.getrusage(resource.RUSAGE_CHILDREN)
        user = after.ru_utime - before.ru_utime
        system = after.ru_stime - before.ru_stime
        row = {
            "case": case,
            "trial": trial,
            "warmup": trial == 0,
            "wall_seconds": elapsed,
            "user_cpu_seconds": user,
            "system_cpu_seconds": system,
            "cpu_seconds": user + system,
            "host_busy_cores": (user + system) / elapsed,
            "context_switches": (after.ru_nvcsw + after.ru_nivcsw)
            - (before.ru_nvcsw + before.ru_nivcsw),
            "exit_code": 0,
            "command": command,
        }
        (output / "result.json").write_text(json.dumps(row, indent=2) + "\n")
        print(
            f"{case:14} trial={trial} wall={elapsed:.2f}s CPU={user + system:.2f}s",
            flush=True,
        )
        return row


def main(args):
    drivers = dict(item.split("=", 1) for item in args.driver)
    cases = args.cases or list(drivers)
    if set(cases) - drivers.keys():
        raise ValueError("unknown case")
    args.output = args.output.resolve()
    args.output.mkdir(parents=True, exist_ok=False)
    if args.cpus:
        os.sched_setaffinity(0, {int(c) for c in args.cpus.split(",")})
    metadata = {
        "date_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "host": {k: v for k, v in platform.uname()._asdict().items() if k != "node"},
        "cpuinfo": Path("/proc/cpuinfo").read_text().split("\n\n")[0],
        "affinity": sorted(os.sched_getaffinity(0)),
        "drivers": drivers,
        "trials": args.trials,
        "warmups_per_case": 1,
        "runtime_filesystem": "private tmpfs; fresh state for every invocation",
    }
    (args.output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    jobs = [(case, trial) for trial in range(1, args.trials + 1) for case in cases]
    random.Random(42).shuffle(jobs)
    jobs = [(case, 0) for case in cases] + jobs
    rows = []
    for index, (case, trial) in enumerate(jobs):
        rows.append(run(args, drivers, case, trial, index))
        (args.output / "measurements.json").write_text(
            json.dumps(rows, indent=2) + "\n"
        )
    summary = []
    for case in cases:
        selected = [r for r in rows if r["case"] == case and not r["warmup"]]
        summary.append(
            {
                "case": case,
                "trials": len(selected),
                **{
                    key: statistics.median(r[key] for r in selected)
                    for key in (
                        "wall_seconds",
                        "cpu_seconds",
                        "host_busy_cores",
                        "context_switches",
                    )
                },
                "wall_min": min(r["wall_seconds"] for r in selected),
                "wall_max": max(r["wall_seconds"] for r in selected),
            }
        )
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--driver", action="append", required=True, help="NAME=/path/to/driver"
    )
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--cases", type=lambda s: s.split(","))
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--cpus")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.trials <= 0:
        parser.error("--trials must be positive")
    main(args)
