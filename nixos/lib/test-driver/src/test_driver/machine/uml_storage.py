"""Persistent UML root images exported by QEMU without a guest CPU."""

import json
import shutil
import socket
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, BinaryIO

from pydantic import BaseModel


class UmlRootDiskConfiguration(BaseModel):
    image: Path
    qemu: Path


class UmlRootDisk:
    def __init__(
        self,
        config: UmlRootDiskConfiguration,
        state_dir: Path,
        out_dir: Path,
        name: str,
        log: Callable[[str], None],
    ) -> None:
        self.config = config
        self.image = state_dir / "root.qcow2"
        self.transport = state_dir / "root.sock"
        self.qmp_path = state_dir / "root.qmp"
        self.log_path = out_dir / f"{name}-root-disk.log"
        self.proof_path = out_dir / f"{name}-root-disk.jsonl"
        self.log = log
        self.process: subprocess.Popen | None = None
        self.connection: socket.socket | None = None
        self.reader: BinaryIO | None = None

    def _execute(self, command: str, arguments: dict[str, Any] | None = None) -> Any:
        # A bounded, synchronous session: only one request can be outstanding.
        assert self.connection is not None and self.reader is not None
        self.connection.sendall(
            json.dumps({"execute": command, "arguments": arguments or {}}).encode()
            + b"\n"
        )
        while True:
            line = self.reader.readline()
            if not line:
                raise RuntimeError("UML root disk QMP disconnected")
            reply = json.loads(line)
            if "error" in reply:
                raise RuntimeError(f"UML root disk QMP: {reply['error']}")
            if "return" in reply:
                return reply["return"]

    def start(self) -> str:
        self.stop()
        if not self.image.exists():
            temporary = self.image.with_suffix(".tmp")
            try:
                shutil.copyfile(self.config.image, temporary)
                temporary.replace(self.image)
            finally:
                temporary.unlink(missing_ok=True)
        self.transport.unlink(missing_ok=True)
        self.qmp_path.unlink(missing_ok=True)
        self.log(f"exporting UML root image {self.image}")
        try:
            with self.log_path.open("a") as log:
                self.process = subprocess.Popen(
                    [
                        str(self.config.qemu),
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
                        f"unix:{self.qmp_path},server=on,wait=off",
                        "-blockdev",
                        json.dumps(
                            {
                                "driver": "file",
                                "node-name": "file",
                                "filename": str(self.image),
                                "cache": {"direct": False, "no-flush": False},
                            }
                        ),
                        "-blockdev",
                        "driver=qcow2,node-name=root,file=file,cache.no-flush=off",
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            deadline = time.monotonic() + 10
            while not self.qmp_path.exists():
                if self.process.poll() is not None:
                    raise RuntimeError(f"UML root backend exited; see {self.log_path}")
                if time.monotonic() > deadline:
                    raise TimeoutError("waiting for UML root disk QMP socket")
                time.sleep(0.05)
            self.connection = socket.socket(socket.AF_UNIX)
            self.connection.settimeout(10)
            self.connection.connect(str(self.qmp_path))
            self.reader = self.connection.makefile("rb")
            if "QMP" not in json.loads(self.reader.readline()):
                raise RuntimeError("invalid UML root disk QMP greeting")
            self._execute("qmp_capabilities")
            cpus = self._execute("query-cpus-fast")
            if cpus != []:
                raise RuntimeError("UML storage backend unexpectedly has guest CPUs")
            self._execute(
                "block-export-add",
                {
                    "type": "vhost-user-blk",
                    "id": "uml-root",
                    "node-name": "root",
                    "writable": True,
                    "addr": {"type": "unix", "path": str(self.transport)},
                },
            )
            exports = self._execute("query-block-exports")
            if len(exports) != 1 or exports[0]["type"] != "vhost-user-blk":
                raise RuntimeError(f"unexpected UML root exports: {exports}")
            with self.proof_path.open("a") as proof:
                proof.write(json.dumps({"cpus": cpus, "exports": exports}) + "\n")
            return f"virtio_uml.device={self.transport}:2"
        except BaseException:
            self.stop()
            raise

    def stop(self) -> None:
        try:
            if self.process is not None and self.process.poll() is None:
                if self.connection is not None and self.reader is not None:
                    try:
                        self._execute("quit")
                    except (OSError, ValueError, RuntimeError) as error:
                        self.log(f"UML root backend quit failed: {error}")
                        self.process.terminate()
                else:
                    self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            elif self.process is not None:
                self.process.wait()
        finally:
            if self.reader is not None:
                self.reader.close()
                self.reader = None
            if self.connection is not None:
                self.connection.close()
                self.connection = None
            self.process = None
            self.transport.unlink(missing_ok=True)
            self.qmp_path.unlink(missing_ok=True)
