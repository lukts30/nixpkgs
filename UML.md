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

## Multi-machine tests with passt

```sh
# Reuse the existing two-machine Git daemon test over TCP; also check UDP
# both ways and clone again after restarting the client and its passt process.
nix-build -A nixosTests.user-mode-linux.gitdaemon -o result-uml-gitdaemon
nix-build --check -A nixosTests.user-mode-linux.gitdaemon --no-out-link

# Interactive driver, in the same isolated network setup.
nix-build -A nixosTests.user-mode-linux.gitdaemon.driverInteractive -o result-uml-git-driver
./result-uml-git-driver/bin/nixos-test-driver
```

Enable `umlDefaults.virtualisation.uml.networking.enable = true` when adapting
a test. The runner starts one passt vhost-user backend per guest. Guests keep
their usual test names and `192.168.<vlan>.<node>` addresses. Allowed TCP/UDP
firewall ports and ranges are forwarded; add otherwise undeclared listeners via
`virtualisation.uml.networking.extraTCPPorts` / `extraUDPPorts` on that guest.
The wrapper's [source](nixos/tests/user-mode-linux-gitdaemon.nix) is an example.

This needs **unprivileged user and network namespaces**, without root or KVM.
Each invocation has a private network namespace containing its guest-address
aliases and passt listeners. It has no external network route. Passt logs are
saved as `<machine>-passt.log` in the test output directory.

Supported scope: UML-only tests, one automatically addressed IPv4 interface per
guest, all on one test VLAN. This is TCP/UDP forwarding, not an Ethernet switch:
do not use it to validate ping, broadcast, DHCP, routing, VLAN isolation or
arbitrary IP protocols. Networking stays opt-in; other UML tests use loopback.

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

UML also boots with its **root filesystem on a block image**:

```sh
nix-build -A user-mode-linux.tests.blockRoot -o result-uml-root
nix-build -A user-mode-linux.tests.qemuBlockRoot -o result-uml-qemu-root
cat result-uml-qemu-root/qmp-1.json
```

Both tests boot a self-contained ext4 image with static BusyBox twice, verify a
persistent counter and file, and run `e2fsck`. There is **no initrd, hostfs or
host `/nix/store`** in these guests. UBD uses `ubd0=/path/root.raw root=/dev/ubda`;
virtio-blk uses `virtio_uml.device=/path/block.sock:2 root=/dev/vda rootwait`.
Both also pass `rw rootfstype=ext4 init=/init`.

The second test is a non-network use of **QEMU `-machine none -nodefaults`**:
QMP `block-export-add` exports a writable qcow2 node as `vhost-user-blk`.
`query-cpus-fast` must return `[]`, and `query-block-exports` must show the
export. This uses `qemu-system-x86_64`, not `qemu-storage-daemon`; no guest CPU
executes inside QEMU. Qcow2 and ext4 checks both passed after the second boot.
The reproducible [test runner](pkgs/by-name/us/user-mode-linux/test-block-root.py)
contains the exact QMP commands.

Current runner limits: the networking scope above; no graphical input,
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
| Shared guest RAM with a daemon | No daemon; host reads/writes fill UML's page cache | Yes; FD passing and `MAP_SHARED` for queues and I/O buffers |
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

**Shared RAM works independently of DAX.** A traced test confirmed that UML
passes its RAM backing FD to virtiofsd using `VHOST_USER_SET_MEM_TABLE` and
`SCM_RIGHTS`; virtiofsd mapped about 503 MiB of a 512 MiB guest with `MAP_SHARED`.
The Unix socket carries setup and file descriptors; requests and data use shared
memory. DAX would additionally map exported host file pages into a guest window.
Hostfs instead uses host `pread`/`pwrite` calls and the guest page cache, including
for ordinary guest `mmap`; it provides no equivalent DAX mapping.

This Linux 6.18 UML backs guest RAM with an **anonymous temporary file under
`TMPDIR`** (`O_TMPFILE`, with a create-and-unlink fallback). It prefers tmpfs when
choosing a directory itself; a selected directory must permit executable mappings.
`memfd_create()` is a modern alternative for anonymous shared RAM, with optional
sealing and no directory dependency. It would be a reasonable choice for a new
implementation, but is not required by vhost-user and is not used for RAM here.

### Efficiency and double caching

For ordinary buffered file I/O in this UML kernel:

| Configuration | Guest file-data cache | Host file-data cache |
| --- | --- | --- |
| hostfs | Yes | Yes |
| virtio-fs, `--cache=auto` or `always` | Yes | Yes |
| virtio-fs, `--cache=never` or `metadata` | Bypassed for regular-file reads/writes | Yes |

Hostfs fills guest cache pages through host `pread` and writes them back through
`pwrite` (`fs/hostfs/hostfs_kern.c` and `hostfs_user.c`). Virtio-fs ordinarily
fills guest FUSE cache pages through virtqueues and virtiofsd. Both paths can
therefore cache the same file data twice. Mapping guest RAM into virtiofsd does
not copy that RAM again, but it also does not merge the two filesystem caches.
UML currently has no DAX window to share the exported file's host pages directly.

Hostfs has the shorter path and no daemon/virtqueue overhead, so I expect it to
be cheaper for many UML workloads; this is an architectural expectation, **not
a measured performance result**. Virtio-fs provides more filesystem features and
cache controls. Bypassing guest caching can save memory but makes repeated reads
go through the daemon. `--cache=never --allow-mmap` needs special care: enabling
mmap reintroduces guest-cached mappings and requires exclusive access according
to virtiofsd. It is not a blanket solution to duplicate caching for executable
files. See [virtiofsd cache policies](https://virtio-fs.gitlab.io/virtiofsd/doc/virtiofsd/passthrough/enum.CachePolicy.html)
and its [mmap option](https://gitlab.com/virtio-fs/virtiofsd#usage).

## Network and device backends

These standalone UML tests passed without root or KVM:

| Backend | UML connection | Verified |
| --- | --- | --- |
| slirp4netns 1.3.6 | `vec0:transport=bess,dst=/path/to/socket,depth=128,gro=1` | HTTP/TCP and UDP, guest→host and forwarded host→guest |
| passt 2026_07_16 | `virtio_uml.device=/path/to/socket:1` | Same TCP/UDP checks through virtio-net |
| rust-vmm vhost-device-vsock 0.3.0 | `virtio_uml.device=/path/to/socket:19` | Guest `AF_VSOCK` ↔ host Unix sockets, both directions |
| QEMU 11.1.2 vhost-user-bridge (patched) | `virtio_uml.device=/path/to/socket:1` | HTTP/TCP and UDP both ways, through QEMU's network hub and slirp |

```sh
nix-build -A user-mode-linux.tests.slirp -A user-mode-linux.tests.passt \
  -A user-mode-linux.tests.vsock -o result-uml-backends

# Run outside the Nix build sandbox; select slirp, passt or vsock.
nix-build -A user-mode-linux.tests.slirp.runner -o result-uml-backend-runner
./result-uml-backend-runner/bin/uml-backend-test passt /tmp/uml-passt-results
```

Start slirp4netns with `--target-type=bess`, or passt with `--vhost-user` and
`--socket=/path/to/socket`. The tests configure `10.0.2.100/24`, gateway
`10.0.2.2`, and only contact local test servers. Internet access and DNS are not
covered. The NixOS `umls` runner can now opt into passt as described above.

### QEMU networking without a guest CPU

```sh
nix-build -A user-mode-linux.tests.qemuNetwork -o result-uml-qemu-network
cat result-uml-qemu-network/result.txt

# The same check as a normal user, outside the build sandbox.
nix-build -A user-mode-linux.tests.qemuNetwork.runner -o result-uml-qemu-runner
./result-uml-qemu-runner/bin/uml-qemu-network-test /tmp/uml-qemu-results
```

The tested path is UML virtio-net → vhost-user Unix socket → QEMU's standalone
`vhost-user-bridge` → Ethernet frames over loopback UDP → QEMU network hub and
slirp. QEMU runs with `-machine none -nodefaults`: no guest CPU, firmware, kernel
or KVM. The bridge maps UML's guest RAM for virtqueues; QEMU's hub receives
Ethernet frames. The test checks HTTP/TCP and UDP in both directions.

Upstream calls this bridge a development tool and does not install it. The test
builds its target separately and applies a small
[feature-advertisement fix](pkgs/by-name/us/user-mode-linux/qemu-vhost-user-bridge-features.patch):
unmodified QEMU 11.1.2 advertises `GUEST_ANNOUNCE` without `CTRL_VQ`, which
Linux's virtio-net driver rejects. The patch removes the unsupported feature.

**QEMU without guest CPU execution:** the Xen term is *device model*
(`xenpvh`/`-xen-attach`); it requires Xen interfaces. For UML, use standalone
**vhost-user backends**, such as the already tested `qemu-storage-daemon`,
virtiofsd, passt, and rust-vmm's vsock daemon. QEMU's `-device vhost-user-*`
options are frontend consumers, not device exporters. Other rust-vmm/QEMU
backends (RNG, console, sound, GPU, input, SCSI, etc.) need their guest drivers
and protocol features checked individually; they have not all been tested here.

Arbitrary emulated PCI devices use a different interface: QEMU has experimental
`x-remote`/`x-vfio-user-server` support. UML's `UML_PCI_OVER_VFIO` accesses host
`/dev/vfio` devices; it is not a vfio-user socket client. UML also has experimental
PCI-over-virtio with an unassigned device ID. Neither provides a ready-made
connection to arbitrary QEMU PCI device models in this branch.

### Other vhost-user projects

| Project | Backend types |
| --- | --- |
| [crosvm](https://www.crosvm.dev/book/devices/vhost_user.html) | Standalone virtio backends via `crosvm devices`, including block and net |
| [SPDK](https://spdk.io/doc/vhost.html) | Polling storage backends for virtio-blk and virtio-scsi |
| [DPDK](https://doc.dpdk.org/guides/prog_guide/vhost_lib.html) | vhost library and network backend examples |
| [Open vSwitch with DPDK](https://docs.openvswitch.org/en/latest/topics/dpdk/vhost-user/) | Network switching with vhost-user ports |
| [rust-vmm vhost-device](https://github.com/rust-vmm/vhost-device) | Vsock, RNG, console, sound, GPU, input and other device daemons |

Of these, only rust-vmm's vsock backend has been exercised with UML here.
Each additional backend needs matching guest drivers, compatible feature
negotiation, and the correct socket role. UML connects as a socket client;
Open vSwitch's preferred `dpdkvhostuserclient` mode also connects, so it cannot
be plugged directly into UML's current transport. SPDK's polling also trades
host CPU time for latency/throughput. These are not interchangeable drop-ins.

References: [slirp4netns BESS mode](https://github.com/rootless-containers/slirp4netns/blob/v1.3.6/slirp4netns.1.md#bess-mode-for-user-mode-linux),
[rust-vmm devices](https://github.com/rust-vmm/vhost-device),
[QEMU backend daemons](https://www.qemu.org/docs/master/system/devices/virtio/vhost-user-contrib.html),
[Xen device model](https://www.qemu.org/docs/master/system/i386/xenpvh.html),
[vfio-user](https://www.qemu.org/docs/master/system/devices/vfio-user.html).
