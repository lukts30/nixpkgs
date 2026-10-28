# UML quick start

Run these commands from this checkout on **x86-64 Linux**. No root or KVM is needed.

## Changes

- `user-mode-linux`: packaged kernel, C/Rust modules, boot test and interactive BusyBox demo.
- `f6862ee8bcc9`: virtio block devices over Unix sockets; qcow2 test using `qemu-storage-daemon`.
- `121cda84d652`: NixOS test-driver backend using `umls`, with existing lighttpd and Grafana tests.

## Run web-service tests

```sh
# Boot NixOS, start lighttpd, check an HTTP response, reload it, and shut down.
nix-build -A nixosTests.user-mode-linux.lighttpd -o result-uml-http

# Run the existing five-guest Grafana suite.
nix-build -A nixosTests.user-mode-linux.grafana -o result-uml-grafana

# Force an already-built test to execute again instead of returning its cached result.
nix-build --check -A nixosTests.user-mode-linux.lighttpd --no-out-link
```

For an interactive session:

```sh
nix-build -A nixosTests.user-mode-linux.lighttpd.driverInteractive -o result-uml-driver
./result-uml-driver/bin/nixos-test-driver
```

At the Python prompt:

```python
start_all()
server.wait_for_unit("lighttpd.service")
print(server.succeed("curl --fail http://localhost/file.txt"))
server.shutdown()
```

To adapt another service test, put its NixOS machine configurations under `umls`
instead of `nodes` or `containers`, and reuse its `testScript`. See
[the lighttpd wrapper](nixos/tests/user-mode-linux.nix).

## Storage and modules

```sh
nix-build -A user-mode-linux.tests.boot    # Includes C and Rust module loading.
nix-build -A user-mode-linux.tests.virtio  # Read/write qcow2 via a vhost-user Unix socket.
nix-build -A user-mode-linux.tests.virtiofs -o result-uml-fs
cat result-uml-fs/comparison.txt
```

The NixOS runner uses a fresh **tmpfs root**, read-only **hostfs `/nix/store`**,
and writable **hostfs `/tmp/shared`** for file transfers. It needs no root disk
image. The separate virtio test mounts a real guest block device, `/dev/vda`.

Current runner limits: loopback networking only; no test VLANs, graphical input,
QEMU monitor or SSH backdoor. The reused Grafana suite leaves Grafana using
SQLite, even in its PostgreSQL/MariaDB cases.

## Virtio-fs versus hostfs

Virtio-fs works over a vhost-user Unix socket: start `virtiofsd` with
`--tag=uml-share`, add `virtio_uml.device=/path/to/socket:26` to UML, then run
`mount -t virtiofs uml-share /mnt` in the guest. Both filesystems passed the
same read/write, shared mmap, link, rename, chmod, guest-process locking,
host-update and read-only mount checks.

| | hostfs | virtio-fs |
| --- | --- | --- |
| Host setup | Direct host syscalls | `virtiofsd` plus Unix socket |
| Ownership in this test | Host UID/GID visible | Guest root mapped to the host user |
| `user.*` xattrs | Unsupported | Supported with `--xattr` |
| Read-only export | Guest read-only mount | Daemon `--readonly` also tested |
| NixOS UML runner | Used for store and file exchange | Available in kernel; runner still uses hostfs |

Nix's build sandbox blocks xattrs. Run the same test as your normal user to
exercise them on a host filesystem that supports them:

```sh
nix-build -A user-mode-linux.tests.virtiofs.runner -o result-uml-fs-runner
./result-uml-fs-runner/bin/uml-virtiofs-test /tmp/uml-fs-results
cat /tmp/uml-fs-results/comparison.txt
```

The test uses temporary directories, `--cache=auto`, UID/GID translation and
`--sandbox=none`; root and user namespaces are unnecessary. Ordinary mmap works,
but this UML transport has no virtio-fs DAX window. Performance and host/guest
lock coordination have not been compared.
