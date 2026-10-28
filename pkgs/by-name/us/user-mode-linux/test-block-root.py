"""Boot an entirely self-contained ext4 root twice, using UBD or QEMU vhost-user."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import time


def wait_path(path, process):
    deadline = time.monotonic() + 15
    while not path.exists():
        if process.poll() is not None:
            raise RuntimeError(f"backend exited with {process.returncode}")
        if time.monotonic() > deadline:
            raise TimeoutError(str(path))
        time.sleep(0.02)


def stop(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class QMP:
    def __init__(self, connection):
        self.file = connection.makefile("rwb", buffering=0)
        assert "QMP" in json.loads(self.file.readline())
        self.execute("qmp_capabilities")

    def execute(self, command, arguments=None):
        self.file.write(
            json.dumps({"execute": command, "arguments": arguments or {}}).encode()
            + b"\n"
        )
        while True:
            reply = json.loads(self.file.readline())
            if "error" in reply:
                raise RuntimeError(reply)
            if "return" in reply:
                return reply["return"]


def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uml-root-") as directory:
        work = Path(directory)
        tree = work / "root"
        for name in ["bin", "dev", "proc", "sys", "tmp", "etc"]:
            (tree / name).mkdir(parents=True)
        shutil.copyfile(args.busybox, tree / "bin/busybox")
        (tree / "bin/busybox").chmod(0o755)
        (tree / "bin/sh").symlink_to("busybox")
        (tree / "etc/root-marker").write_text("self-contained UML block root\n")
        (tree / "init").write_text("""#!/bin/sh
set -eu
export PATH=/bin
/bin/busybox --install -s /bin
mount -t proc proc /proc
mount -t sysfs sysfs /sys
grep -q "devtmpfs /dev devtmpfs" /proc/mounts
test "$$" = 1
test ! -e /nix/store
grep -q 'self-contained UML block root' /etc/root-marker
awk '$2 == "/" && $3 == "ext4" { found = 1; print } END { exit !found }' /proc/mounts
count=0
if test -e /boot-count; then count=$(cat /boot-count); fi
count=$((count + 1))
echo "$count" > /boot-count
echo "persistent root write $count" > /persist
sync
mount -o remount,ro /
echo "UML_BLOCK_ROOT_SUCCESS boot=$count"
poweroff -f
""")
        (tree / "init").chmod(0o755)
        raw = work / "root.raw"
        with raw.open("wb") as file:
            file.truncate(64 * 1024 * 1024)
        subprocess.run(["mkfs.ext4", "-q", "-d", str(tree), str(raw)], check=True)
        disk = raw
        if args.mode == "qemu":
            disk = work / "root.qcow2"
            subprocess.run(
                [
                    "qemu-img",
                    "convert",
                    "-f",
                    "raw",
                    "-O",
                    "qcow2",
                    str(raw),
                    str(disk),
                ],
                check=True,
            )
            raw.unlink()
        for boot in (1, 2):
            with ExitStack() as stack:
                runtime = work / f"runtime-{boot}"
                runtime.mkdir()
                device = [f"ubd0={disk}", "root=/dev/ubda"]
                if args.mode == "qemu":
                    qmp_socket = runtime / "qmp.sock"
                    block_socket = runtime / "block.sock"
                    log = stack.enter_context((output / f"qemu-{boot}.log").open("w"))
                    qemu = subprocess.Popen(
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
                                }
                            ),
                            "-blockdev",
                            "driver=qcow2,node-name=disk,file=file",
                        ],
                        stdout=log,
                        stderr=subprocess.STDOUT,
                    )
                    stack.callback(stop, qemu)
                    wait_path(qmp_socket, qemu)
                    connection = stack.enter_context(socket.socket(socket.AF_UNIX))
                    connection.settimeout(15)
                    connection.connect(str(qmp_socket))
                    qmp = QMP(connection)
                    stack.callback(qmp.file.close)
                    cpus = qmp.execute("query-cpus-fast")
                    assert cpus == [], cpus
                    qmp.execute(
                        "block-export-add",
                        {
                            "type": "vhost-user-blk",
                            "id": "uml-root",
                            "node-name": "disk",
                            "writable": True,
                            "addr": {"type": "unix", "path": str(block_socket)},
                        },
                    )
                    (output / f"qmp-{boot}.json").write_text(
                        json.dumps(
                            {
                                "cpus": cpus,
                                "exports": qmp.execute("query-block-exports"),
                            },
                            indent=2,
                        )
                        + "\n"
                    )
                    wait_path(block_socket, qemu)
                    device = [
                        f"virtio_uml.device={block_socket}:2",
                        "root=/dev/vda",
                        "rootwait",
                    ]
                with (output / f"boot-{boot}.log").open("w") as log:
                    subprocess.run(
                        [
                            args.kernel,
                            "mem=256M",
                            "rw",
                            "rootfstype=ext4",
                            "init=/init",
                            "con=null",
                            "con0=fd:0,fd:1",
                            "ssl=null",
                            f"uml_dir={runtime}",
                            *device,
                        ],
                        env=os.environ | {"TMPDIR": str(runtime)},
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=90,
                        check=True,
                    )
                assert (
                    f"UML_BLOCK_ROOT_SUCCESS boot={boot}"
                    in (output / f"boot-{boot}.log").read_text()
                )
        if args.mode == "qemu":
            with (output / "qcow2-check.log").open("w") as log:
                subprocess.run(
                    ["qemu-img", "check", str(disk)],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )
            subprocess.run(
                [
                    "qemu-img",
                    "convert",
                    "-f",
                    "qcow2",
                    "-O",
                    "raw",
                    str(disk),
                    str(raw),
                ],
                check=True,
            )
        with (output / "fsck.log").open("w") as log:
            subprocess.run(
                ["e2fsck", "-fn", str(raw)],
                stdout=log,
                stderr=subprocess.STDOUT,
                check=True,
            )
        data = subprocess.check_output(
            ["debugfs", "-R", "cat /persist", str(raw)], text=True
        )
        assert data.strip() == "persistent root write 2", data
        (output / "result.txt").write_text(
            f"{args.mode}: ext4 root, no initrd/hostfs, two boots, persistence and fsck PASS\n"
        )
        print((output / "result.txt").read_text(), end="")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for flag in ["kernel", "busybox", "qemu"]:
        parser.add_argument(f"--{flag}", required=True)
    parser.add_argument("mode", choices=["ubd", "qemu"])
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        run(args)
    except BaseException:
        for log in args.output.glob("*.log"):
            print(f"{log.name}:\n{log.read_text()}", flush=True)
        raise
