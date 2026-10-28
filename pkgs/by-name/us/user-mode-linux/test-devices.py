"""Exercise RNG, console and two SCSI LUNs over UML vhost-user Unix sockets."""

import argparse
import contextlib
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import tempfile
import termios
import time
import tty


def payload(name, size=65536):
    return hashlib.shake_256(name.encode()).digest(size)


def console_payload(name):
    # This backend implements a terminal: Ctrl-C exits it, and guest output
    # must be UTF-8. Exercise many buffers without invoking terminal commands.
    return payload(name, 32768).hex().encode()


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def wait_for(condition, processes=(), timeout=60):
    deadline = time.monotonic() + timeout
    while not condition():
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(f"process exited early: {process.args}")
        if time.monotonic() > deadline:
            raise TimeoutError("waiting for device test")
        time.sleep(0.05)


def read_exact(fd, size, timeout=30):
    result = bytearray()
    deadline = time.monotonic() + timeout
    while len(result) < size:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
            raise TimeoutError(f"reading {size} console bytes, got {len(result)}")
        data = os.read(fd, size - len(result))
        if not data:
            raise EOFError("console disconnected")
        result.extend(data)
    return bytes(result)


def write_all(fd, data, timeout=30):
    deadline = time.monotonic() + timeout
    while data:
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([], [fd], [], remaining)[1]:
            raise TimeoutError("writing console data")
        try:
            data = data[os.write(fd, data[:4096]) :]
        except BlockingIOError:
            pass


def guest(control):
    config = json.loads((control / "config.json").read_text())
    wait_for(lambda: Path("/dev/hwrng").exists())
    rng_sysfs = Path("/sys/class/misc/hw_random")
    choices = [
        name
        for name in (rng_sysfs / "rng_available").read_text().split()
        if name.startswith("virtio_rng")
    ]
    assert len(choices) == 1, choices
    # UML also has a native host RNG driver; explicitly select vhost-user.
    (rng_sysfs / "rng_current").write_text(choices[0])
    rng_name = (rng_sysfs / "rng_current").read_text().strip()
    assert rng_name == choices[0], rng_name
    with open("/dev/hwrng", "rb", buffering=0) as rng:
        sample = bytearray()
        while len(sample) < 65536:
            block = rng.read(65536 - len(sample))
            assert block, "empty RNG response"
            sample.extend(block)
    assert len(set(sample)) > 128
    assert sample[:32768] != sample[32768:]
    print(f"RNG: {rng_name}, 65536 nonconstant bytes PASS", flush=True)

    for index, disk in enumerate(["sda", "sdb"]):
        device = Path("/dev") / disk
        wait_for(device.exists)
        sysfs = Path("/sys/block") / disk
        assert (sysfs / "ro").read_text().strip() == "1"
        assert int((sysfs / "size").read_text()) == config["disk_sectors"]
        assert (sysfs / "queue/rotational").read_text().strip() == "0"
        try:
            fd = os.open(device, os.O_WRONLY)
        except OSError as error:
            assert error.errno == errno.EROFS, error
        else:
            os.close(fd)
            raise AssertionError(f"{device} unexpectedly writable")
        mount = Path(f"/mnt/lun{index}")
        mount.mkdir(parents=True)
        subprocess.run(
            ["mount", "-t", "ext4", "-o", "ro,noload", device, mount], check=True
        )
        assert (mount / "label").read_text() == f"UML SCSI LUN {index}\n"
        assert digest(mount / "payload") == config["disk_hashes"][index]
        subprocess.run(["umount", mount], check=True)
        print(
            f"SCSI: {device}, LUN {index}, payload and read-only enforcement PASS",
            flush=True,
        )

    wait_for(lambda: Path("/dev/hvc0").exists())
    with open("/dev/hvc0", "r+b", buffering=0) as console:
        fd = console.fileno()
        tty.setraw(fd)
        os.set_blocking(fd, False)
        (control / "guest-ready").touch()
        write_all(fd, b"UML_CONSOLE_READY\n")
        assert read_exact(fd, 65536) == console_payload("host-to-guest")
        write_all(fd, console_payload("guest-to-host"))
    print("CONSOLE: 65536 ASCII bytes in each direction via /dev/hvc0 PASS", flush=True)
    wait_for(lambda: (control / "host-complete").exists())
    (control / "result.json").write_text(
        json.dumps(
            {
                "rng": {"driver": rng_name, "bytes": 65536},
                "console": {
                    "device": "/dev/hvc0",
                    "bytes_each_direction": 65536,
                    "encoding": "ascii",
                },
                "scsi": {
                    "luns": 2,
                    "read_only": True,
                    "payload_hashes": config["disk_hashes"],
                },
            },
            indent=2,
        )
        + "\n"
    )


def stop(process):
    # UML uses traced child processes; terminate the whole isolated session.
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()


def console_session():
    os.setsid()
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)


def host(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uml-devices-") as directory:
        work = Path(directory)
        control = work / "control"
        control.mkdir()
        try:
            with contextlib.ExitStack() as stack:

                def start(command, name, **kwargs):
                    log = stack.enter_context((output / name).open("wb"))
                    options = {
                        "stdin": subprocess.DEVNULL,
                        "stdout": log,
                        "stderr": log,
                        "start_new_session": True,
                    }
                    options.update(kwargs)
                    process = subprocess.Popen(command, **options)
                    stack.callback(stop, process)
                    return process

                disks = []
                payload_hashes = []
                for index in range(2):
                    tree = work / f"tree{index}"
                    tree.mkdir()
                    (tree / "label").write_text(f"UML SCSI LUN {index}\n")
                    (tree / "payload").write_bytes(payload(f"lun{index}", 262144))
                    payload_hashes.append(digest(tree / "payload"))
                    disk = work / f"lun{index}.raw"
                    with disk.open("wb") as stream:
                        stream.truncate(32 * 1024 * 1024)
                    subprocess.run(
                        [
                            args.mkfs,
                            "-q",
                            "-F",
                            "-E",
                            "lazy_itable_init=0,lazy_journal_init=0",
                            "-d",
                            tree,
                            disk,
                        ],
                        check=True,
                    )
                    disks.append(disk)
                disk_hashes = [digest(disk) for disk in disks]
                (control / "config.json").write_text(
                    json.dumps(
                        {
                            "disk_sectors": 32 * 1024 * 1024 // 512,
                            "disk_hashes": payload_hashes,
                        }
                    )
                )

                rng_socket = work / "rng.sock0"
                rng = start(
                    [
                        str(args.backends / "vhost-device-rng"),
                        "--socket-path",
                        str(work / "rng.sock"),
                        "--period",
                        "1000",
                        "--max-bytes",
                        "1048576",
                    ],
                    "rng.log",
                )
                scsi_socket = work / "scsi.sock"
                scsi = start(
                    [
                        str(args.backends / "vhost-device-scsi"),
                        "--socket-path",
                        str(scsi_socket),
                        "--read-only",
                        "--solid-state",
                        *(str(disk) for disk in disks),
                    ],
                    "scsi.log",
                )
                master, slave = pty.openpty()
                stack.callback(os.close, master)
                stack.callback(os.close, slave)
                tty.setraw(slave)
                os.set_blocking(master, False)
                console_socket = work / "console.sock0"
                console = start(
                    [
                        str(args.backends / "vhost-device-console"),
                        "--socket-path",
                        str(work / "console.sock"),
                        "--backend",
                        "nested",
                        "--max-queue-size",
                        "256",
                    ],
                    "console.log",
                    stdin=slave,
                    stdout=slave,
                    start_new_session=False,
                    preexec_fn=console_session,
                )
                daemons = [rng, scsi, console]
                wait_for(
                    lambda: all(
                        p.exists() for p in [rng_socket, scsi_socket, console_socket]
                    ),
                    daemons,
                )
                kernel = start(
                    [
                        args.kernel,
                        "mem=512M",
                        f"initrd={args.initrd}",
                        "con=null",
                        "con0=fd:0,fd:1",
                        "ssl=null",
                        f"uml_dir={work}",
                        f"uml_control={control}",
                        f"virtio_uml.device={rng_socket}:4",
                        f"virtio_uml.device={console_socket}:3",
                        f"virtio_uml.device={scsi_socket}:8",
                    ],
                    "boot.log",
                    env=os.environ | {"TMPDIR": str(work)},
                )
                wait_for(
                    lambda: (control / "guest-ready").exists(),
                    [kernel, *daemons],
                    timeout=120,
                )
                assert (
                    read_exact(master, len(b"UML_CONSOLE_READY\n"))
                    == b"UML_CONSOLE_READY\n"
                )
                write_all(master, console_payload("host-to-guest"))
                assert read_exact(master, 65536) == console_payload("guest-to-host")
                (control / "host-complete").touch()
                assert kernel.wait(timeout=30) == 0
                assert "UML_DEVICES_SUCCESS" in (control / "guest.log").read_text()
                assert [digest(disk) for disk in disks] == disk_hashes, (
                    "SCSI images changed"
                )
                shutil.copyfile(control / "result.json", output / "result.json")
        except BaseException:
            for log in [*output.glob("*.log"), control / "guest.log"]:
                if log.exists():
                    print(f"{log.name}:\n{log.read_text(errors='replace')}", flush=True)
            raise
        finally:
            if (control / "guest.log").exists():
                shutil.copyfile(control / "guest.log", output / "guest.log")
        (output / "result.txt").write_text(
            "RNG, ASCII console I/O and two read-only SCSI LUNs: PASS\n"
        )
        print((output / "result.txt").read_text(), end="", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    guest_parser = commands.add_parser("guest")
    guest_parser.add_argument("control", type=Path)
    host_parser = commands.add_parser("host")
    host_parser.add_argument("--kernel", required=True)
    host_parser.add_argument("--initrd", required=True)
    host_parser.add_argument("--backends", required=True, type=Path)
    host_parser.add_argument("--mkfs", required=True)
    host_parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "guest":
        guest(args.control)
    else:
        host(args)
