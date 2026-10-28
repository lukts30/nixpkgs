"""User Mode Linux machines with a serial backdoor and hostfs exchange directory."""

import datetime as dt
import os
import socket
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

from test_driver.duration import Duration
from test_driver.logger import AbstractLogger
from test_driver.machine import SerialMachine


class UmlMachine(SerialMachine):
    def __init__(
        self,
        out_dir: Path,
        name: str,
        logger: AbstractLogger,
        tmp_dir: Path,
        callbacks: list[Callable] | None,
        keep_machine_state: bool,
        start_command: Path,
    ) -> None:
        super().__init__(out_dir, name, logger, tmp_dir, callbacks, keep_machine_state)
        self.start_command = start_command
        self.process: subprocess.Popen | None = None
        self.shell: socket.socket | None = None
        self.serial_thread: threading.Thread | None = None
        self.connected = False
        self.full_console_log: list[str] = []

    def is_up(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.is_up():
            return
        self.release()
        self.log("starting User Mode Linux")
        self.shell, guest = socket.socketpair()
        self.connected = False
        self.full_console_log = []
        try:
            self.process = subprocess.Popen(
                [
                    str(self.start_command),
                    f"ssl0=fd:{guest.fileno()},fd:{guest.fileno()}",
                ],
                env=os.environ
                | {
                    "TMPDIR": str(self.state_dir),
                    "UML_SHARED_DIR": str(self.shared_dir),
                },
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                pass_fds=(guest.fileno(),),
                start_new_session=True,
            )
        finally:
            guest.close()

        def read_console() -> None:
            assert self.process and self.process.stdout
            for raw_line in self.process.stdout:
                line = raw_line.decode(errors="replace").rstrip()
                self.full_console_log.append(line)
                self.log_serial(line)

        self.serial_thread = threading.Thread(target=read_console, daemon=True)
        self.serial_thread.start()

    def get_console_log(self) -> str:
        return "\n".join(self.full_console_log)

    def wait_for_shutdown(self, timeout: dt.timedelta | None = None) -> None:
        if self.process is not None:
            self.process.wait(timeout=timeout.total_seconds() if timeout else None)
        self.connected = False

    def shutdown(self) -> None:
        if self.is_up():
            self.connect()
            assert self.shell
            self.shell.sendall(b"poweroff\n")
            self.wait_for_shutdown(dt.timedelta(seconds=60))

    def release(self) -> None:
        if self.process is not None:
            if self.is_up():
                self.process.terminate()
                try:
                    self.process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            if self.serial_thread is not None:
                self.serial_thread.join(timeout=10)
            if self.process.stdin:
                self.process.stdin.close()
            if self.process.stdout:
                self.process.stdout.close()
            self.process = None
        if self.shell is not None:
            self.shell.close()
            self.shell = None
        self.connected = False

    def send_key(
        self,
        key: str,
        delay: Duration | None = dt.timedelta(milliseconds=10),
        log: bool | None = True,
    ) -> None:
        raise NotImplementedError("UML has no emulated keyboard; use execute()")
