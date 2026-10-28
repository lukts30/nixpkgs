"""Exercise real UML network/vsock devices with unprivileged host backends."""

import argparse
from contextlib import ExitStack
import http.client
import http.server
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time


def wait_for(condition, processes=(), timeout=30):
    deadline = time.monotonic() + timeout
    while not condition():
        for process in processes:
            if process.poll() is not None:
                raise RuntimeError(
                    f"process exited: {process.args} ({process.returncode})"
                )
        if time.monotonic() > deadline:
            raise TimeoutError("waiting for test peer")
        time.sleep(0.05)


def receive(sock, length):
    data = b""
    while len(data) < length:
        part = sock.recv(length - len(data))
        if not part:
            raise RuntimeError("unexpected EOF")
        data += part
    return data


def http_server(address, message):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(message)))
            self.end_headers()
            self.wfile.write(message)

        def log_message(self, *_args):
            pass

    server = http.server.HTTPServer(address, Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def check_http(address, port, expected):
    connection = http.client.HTTPConnection(address, port, timeout=10)
    try:
        connection.request("GET", "/")
        response = connection.getresponse()
        assert response.status == 200
        assert response.read() == expected
    finally:
        connection.close()


def udp_server(address):
    server = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    server.bind(address)

    def echo():
        data, peer = server.recvfrom(4096)
        server.sendto(data.upper(), peer)

    threading.Thread(target=echo, daemon=True).start()
    return server


def check_udp(address, port):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as client:
        client.settimeout(10)
        client.sendto(b"uml udp", (address, port))
        assert client.recv(4096) == b"UML UDP"


def guest(control):
    config = json.loads((control / "config.json").read_text())
    mode = config["mode"]
    if mode == "vsock":
        with socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM) as client:
            client.settimeout(10)
            client.connect((socket.VMADDR_CID_HOST, 1234))
            client.sendall(b"guest-to-host")
            assert receive(client, 7) == b"host-ok"
        print("UML_VSOCK_GUEST_TO_HOST_SUCCESS", flush=True)
        with socket.socket(socket.AF_VSOCK, socket.SOCK_STREAM) as server:
            server.settimeout(20)
            server.bind((socket.VMADDR_CID_ANY, 1235))
            server.listen(1)
            (control / "guest-ready").touch()
            client, _ = server.accept()
            with client:
                client.settimeout(10)
                assert receive(client, 13) == b"host-to-guest"
                client.sendall(b"guest-ok")
        print("UML_VSOCK_HOST_TO_GUEST_SUCCESS", flush=True)
    else:
        interface = "vec0" if mode == "slirp" else "eth0"
        for args in [
            ["link", "set", "lo", "up"],
            ["addr", "add", "10.0.2.100/24", "dev", interface],
            ["link", "set", interface, "up"],
            ["route", "add", "default", "via", "10.0.2.2"],
        ]:
            subprocess.run(["ip", *args], check=True)
        check_http("10.0.2.2", config["http_port"], b"host-http")
        check_udp("10.0.2.2", config["udp_port"])
        print("UML_NETWORK_GUEST_TO_HOST_TCP_UDP_SUCCESS", flush=True)
        http_server(("0.0.0.0", 8080), b"guest-http")
        udp_server(("0.0.0.0", 8081))
        (control / "guest-ready").touch()
    wait_for(lambda: (control / "host-complete").exists())


def free_port(kind):
    with socket.socket(socket.AF_INET, kind) as temporary:
        temporary.bind(("127.0.0.1", 0))
        return temporary.getsockname()[1]


def slirp_forward(path, protocol, port, guest_port):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10)
        client.connect(str(path))
        client.sendall(
            json.dumps(
                {
                    "execute": "add_hostfwd",
                    "arguments": {
                        "proto": protocol,
                        "host_addr": "127.0.0.1",
                        "host_port": port,
                        "guest_addr": "10.0.2.100",
                        "guest_port": guest_port,
                    },
                }
            ).encode()
            + b"\n"
        )
        client.shutdown(socket.SHUT_WR)
        reply = json.loads(client.makefile("rb").read())
        assert "return" in reply, reply


def stop(process):
    # BESS startup forks a helper; also clean it up if UML fails to connect.
    for action in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, action)
        except ProcessLookupError:
            pass
        if action == signal.SIGTERM:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
    process.wait()


def host(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="uml-backend-") as directory:
        work = Path(directory)
        control = work / "control"
        control.mkdir()
        transport = work / "device.sock"
        application = work / "app.sock"
        config = {"mode": args.backend}
        try:
            with ExitStack() as stack:

                def start(command, name, **kwargs):
                    log = stack.enter_context((output / name).open("w"))
                    process = subprocess.Popen(
                        command,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                        **kwargs,
                    )
                    stack.callback(stop, process)
                    return process

                if args.backend == "vsock":
                    listener = stack.enter_context(
                        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    )
                    listener.bind(str(application) + "_1234")
                    listener.listen(1)

                    def serve_vsock():
                        client, _ = listener.accept()
                        with client:
                            client.settimeout(10)
                            if receive(client, 13) == b"guest-to-host":
                                client.sendall(b"host-ok")

                    threading.Thread(target=serve_vsock, daemon=True).start()
                    backend = start(
                        [
                            args.vsock,
                            "--guest-cid=3",
                            f"--socket={transport}",
                            f"--uds-path={application}",
                        ],
                        "backend.log",
                    )
                    device = f"virtio_uml.device={transport}:19"
                else:
                    http = http_server(("127.0.0.1", 0), b"host-http")
                    stack.callback(http.server_close)
                    stack.callback(http.shutdown)
                    udp = stack.enter_context(udp_server(("127.0.0.1", 0)))
                    config.update(
                        http_port=http.server_port, udp_port=udp.getsockname()[1]
                    )
                    tcp_forward = free_port(socket.SOCK_STREAM)
                    udp_forward = free_port(socket.SOCK_DGRAM)
                    if args.backend == "slirp":
                        api = work / "api.sock"
                        backend = start(
                            [
                                args.slirp,
                                "--target-type=bess",
                                f"--api-socket={api}",
                                str(transport),
                            ],
                            "backend.log",
                        )
                        device = f"vec0:transport=bess,dst={transport},depth=128,gro=1"
                    else:
                        backend = start(
                            [
                                args.passt,
                                "--foreground",
                                "--one-off",
                                "--vhost-user",
                                f"--socket={transport}",
                                "--ipv4-only",
                                "--address=10.0.2.100",
                                "--netmask=24",
                                "--gateway=10.0.2.2",
                                "--interface=lo",
                                "--map-host-loopback=10.0.2.2",
                                f"--tcp-ports=127.0.0.1/{tcp_forward}:8080",
                                f"--udp-ports=127.0.0.1/{udp_forward}:8081",
                            ],
                            "backend.log",
                        )
                        device = f"virtio_uml.device={transport}:1"
                wait_for(lambda: transport.exists(), [backend])
                (control / "config.json").write_text(json.dumps(config))
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
                        device,
                    ],
                    "boot.log",
                    env={**os.environ, "TMPDIR": str(work)},
                    stdin=subprocess.DEVNULL,
                )
                wait_for(
                    lambda: (control / "guest-ready").exists(),
                    [kernel, backend],
                    timeout=90,
                )
                if args.backend == "vsock":
                    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                        client.settimeout(10)
                        client.connect(str(application))
                        client.sendall(b"CONNECT 1235\n")
                        reply = b""
                        while not reply.endswith(b"\n"):
                            reply += receive(client, 1)
                        assert reply.startswith(b"OK "), reply
                        client.sendall(b"host-to-guest")
                        assert receive(client, 8) == b"guest-ok"
                else:
                    if args.backend == "slirp":
                        # The BESS helper accepts UML before starting the API.
                        wait_for(lambda: api.exists(), [backend])
                        slirp_forward(api, "tcp", tcp_forward, 8080)
                        slirp_forward(api, "udp", udp_forward, 8081)
                    check_http("127.0.0.1", tcp_forward, b"guest-http")
                    check_udp("127.0.0.1", udp_forward)
                    print("UML_NETWORK_HOST_TO_GUEST_TCP_UDP_SUCCESS", flush=True)
                (control / "host-complete").touch()
                assert kernel.wait(timeout=30) == 0
                assert "UML_BACKEND_SUCCESS" in (control / "guest.log").read_text()
        except BaseException:
            for log in [*output.glob("*.log"), control / "guest.log"]:
                if log.exists():
                    print(f"{log.name}:\n{log.read_text(errors='replace')}", flush=True)
            raise
        finally:
            if (control / "guest.log").exists():
                shutil.copyfile(control / "guest.log", output / "guest.log")
        (output / "result.txt").write_text(
            f"{args.backend}: bidirectional communication PASS\n"
        )
        print((output / "result.txt").read_text(), end="", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    guest_parser = commands.add_parser("guest")
    guest_parser.add_argument("control", type=Path)
    host_parser = commands.add_parser("host")
    for flag in ["kernel", "initrd", "passt", "slirp", "vsock"]:
        host_parser.add_argument(f"--{flag}", required=True)
    host_parser.add_argument("backend", choices=["slirp", "passt", "vsock"])
    host_parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.command == "guest":
        guest(args.control)
    else:
        host(args)
