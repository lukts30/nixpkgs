# Linux kernel {#sec-linux-kernel}

The Nix expressions to build the Linux kernel are in [`pkgs/os-specific/linux/kernel`](https://github.com/NixOS/nixpkgs/blob/master/pkgs/os-specific/linux/kernel).

The function [`pkgs.buildLinux`](https://github.com/NixOS/nixpkgs/blob/d77bda728d5041c1294a68fb25c79e2d161f62b9/pkgs/os-specific/linux/kernel/generic.nix) builds a kernel with [common configuration values](https://github.com/NixOS/nixpkgs/blob/d77bda728d5041c1294a68fb25c79e2d161f62b9/pkgs/os-specific/linux/kernel/common-config.nix).
This is the preferred option unless you have a very specific use case.
Most kernels packaged in Nixpkgs are built that way, and it will also generate kernels suitable for NixOS.
[`pkgs.linuxManualConfig`](https://github.com/NixOS/nixpkgs/blob/d77bda728d5041c1294a68fb25c79e2d161f62b9/pkgs/os-specific/linux/kernel/build.nix) requires a complete configuration to be passed.
It has fewer additional features than `pkgs.buildLinux`, which provides common configuration values and exposes the `features` attribute, as explained below.

Both functions have an argument `kernelPatches` which should be a list of `{name, patch, extraConfig}` attribute sets, where `name` is the name of the patch (which is included in the kernel’s `meta.description` attribute), `patch` is the patch itself (possibly compressed), and `extraConfig` (optional) is a string specifying extra options to be concatenated to the kernel configuration file (`.config`).

The kernel derivation created with `pkgs.buildLinux` exports an attribute `features` specifying whether optional functionality is or isn’t enabled. This is used in NixOS to implement kernel-specific behaviour.

If you are using a kernel packaged in Nixpkgs, you can customize it by overriding its arguments. For details on how each argument affects the generated kernel, refer to [the `pkgs.buildLinux` source code](https://github.com/NixOS/nixpkgs/blob/d77bda728d5041c1294a68fb25c79e2d161f62b9/pkgs/os-specific/linux/kernel/generic.nix).

:::{.example #ex-overriding-kernel-derivation}

# Overriding the kernel derivation

Assuming you are using the kernel from `pkgs.linux_latest`:

```nix
pkgs.linux_latest.override {
  ignoreConfigErrors = true;
  autoModules = false;
  kernelPreferBuiltin = true;
  structuredExtraConfig = with lib.kernel; {
    DEBUG_KERNEL = yes;
    FRAME_POINTER = yes;
    KGDB = yes;
    KGDB_SERIAL_CONSOLE = yes;
    DEBUG_INFO = yes;
  };
}
```

:::

## Manual kernel configuration {#sec-manual-kernel-configuration}

Sometimes it may not be desirable to use kernels built with `pkgs.buildLinux`, especially if most of the common configuration has to be altered or disabled to achieve a kernel as expected by the target use case.
An example of this is building a kernel for use in a VM or micro VM. You can use `pkgs.linuxPackages_custom` in these cases. It requires the `src`, `version`, and `configfile` attributes to be specified.

:::{.example #ex-using-linux-manual-config}

# Using `pkgs.linuxPackages_custom` with a specific source, version, and config file

```nix
{ pkgs, ... }:
pkgs.linuxPackages_custom {
  version = "6.1.55";
  src = pkgs.fetchurl {
    url = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${version}.tar.xz";
    hash = "sha256-qH4kHsFdU0UsTv4hlxOjdp2IzENrW5jPbvsmLEr/FcA=";
  };
  configfile = ./path_to_config_file;
}
```

If necessary, the version string can be slightly modified to explicitly mark it as a custom version. If you do so, ensure the `modDirVersion` attribute matches the source's version, otherwise the build will fail.

```nix
{ pkgs, ... }:
pkgs.linuxPackages_custom {
  version = "6.1.55-custom";
  modDirVersion = "6.1.55";
  src = pkgs.fetchurl {
    url = "https://cdn.kernel.org/pub/linux/kernel/v6.x/linux-${modDirVersion}.tar.xz";
    hash = "sha256-qH4kHsFdU0UsTv4hlxOjdp2IzENrW5jPbvsmLEr/FcA=";
  };
  configfile = ./path_to_config_file;
}
```

:::

Additional attributes can be used with `linuxManualConfig` for further customisation instead of `linuxPackages_custom`. You're encouraged to read [the `pkgs.linuxManualConfig` source code](https://github.com/NixOS/nixpkgs/blob/d77bda728d5041c1294a68fb25c79e2d161f62b9/pkgs/os-specific/linux/kernel/build.nix) to understand how to use them.

To edit the `.config` file for Linux X.Y from within Nix, proceed as follows:

```ShellSession
$ nix-shell '<nixpkgs>' -A linuxKernel.kernels.linux_X_Y.configEnv
$ unpackPhase
$ cd linux-*
$ make nconfig
```

## Developing kernel modules {#sec-linux-kernel-developing-modules}

When developing kernel modules it's often convenient to run the edit-compile-run loop as quickly as possible.
See the snippet below as an example.

:::{.example #ex-edit-compile-run-kernel-modules}

# Edit-compile-run loop when developing `mellanox` drivers

```ShellSession
$ nix-build '<nixpkgs>' -A linuxPackages.kernel.dev
$ nix-shell '<nixpkgs>' -A linuxPackages.kernel
$ unpackPhase
$ cd linux-*
$ make -C $dev/lib/modules/*/build M=$(pwd)/drivers/net/ethernet/mellanox modules
# insmod ./drivers/net/ethernet/mellanox/mlx5/core/mlx5_core.ko
```

:::

## User Mode Linux {#sec-user-mode-linux}

`pkgs.user-mode-linux` builds the upstream Linux kernel as a Linux userspace
executable. It follows the source version of `pkgs.linux` and runs without root
or KVM on x86-64 and i686 Linux hosts.

From a Nixpkgs checkout, build the kernel and run the included BusyBox guest:

```ShellSession
$ nix-build -A user-mode-linux -o result-uml
$ nix-build -A user-mode-linux.demo -o result-uml-demo
$ ./result-uml-demo/bin/uml-demo
```

The demo boots an initramfs, checks module loading, and opens a shell. Exit the
shell to power off. Guest changes are ephemeral. To run the automated boot,
module, virtual disk, hostfs, and management console test:

```ShellSession
$ nix-build -A user-mode-linux.tests.boot -o result-uml-test
$ cat result-uml-test/boot.log
```

For an existing raw ext4 root filesystem with a compatible guest userspace:

```ShellSession
$ ./result-uml/bin/linux mem=512M ubd0=rootfs.ext4 root=/dev/ubda rw con=null con0=fd:0,fd:1 ssl=null umid=my-guest
```

The root image must contain an init system. UML is not an ISA emulator: an
x86-64 UML kernel needs an x86-64 guest userspace. The package enables UML block
devices, hostfs, initramfs, ext4, IPv6, and vector network transports. See the
[upstream UML guide](https://docs.kernel.org/virt/uml/user_mode_linux_howto_v2.html)
for transport configuration and console options. `TMPDIR` must refer to a
writable filesystem that permits executable mappings; set it explicitly if
the host mounts `/tmp` with `noexec`.

`pkgs.uml-utilities` provides `uml_mconsole`, `uml_moo`, `uml_mkcow`, `tunctl`,
`uml_switch`, `uml_net`, and console helpers. For example, with the guest above
running:

```ShellSession
$ nix-shell -I nixpkgs=. -p uml-utilities --run 'uml_mconsole my-guest version'
```

For port or xterm consoles, set `UML_PORT_HELPER` to
`${pkgs.uml-utilities}/lib/uml/port-helper` in the host environment. The package
does not install setuid helpers; network transports requiring host privileges
need a separately configured host interface or appropriate permissions.

### Virtio over Unix sockets {#sec-user-mode-linux-virtio}

The kernel enables `CONFIG_VIRTIO_UML` and `CONFIG_VIRTIO_BLK`. UML can attach
virtio devices directly to vhost-user Unix sockets; it does not need PCI
emulation. For example, export an existing qcow2 disk with
[`qemu-storage-daemon`](https://www.qemu.org/docs/master/tools/qemu-storage-daemon.html):

```sh
qemu-storage-daemon \
  --blockdev driver=file,node-name=file,filename=disk.qcow2 \
  --blockdev driver=qcow2,node-name=disk,file=file \
  --export type=vhost-user-blk,id=uml,addr.type=unix,addr.path=/tmp/uml-block.sock,node-name=disk,writable=on
```

Add `virtio_uml.device=/tmp/uml-block.sock:2` to the UML command line. The `2`
is the virtio block device ID; the guest sees `/dev/vda`. The qcow2 format is
handled by the storage daemon on the host. Stop the daemon after shutting
down the guest before inspecting or modifying the image from the host.

The integration test creates an ext4 image, converts it to qcow2, exports it,
checks guest reads and writes, and verifies the image and written data after
shutdown:

```ShellSession
$ nix-build -A user-mode-linux.tests.virtio -o result-uml-virtio
$ cat result-uml-virtio/boot.log
$ cat result-uml-virtio/qcow2-check.log
```

The kernel also enables `CONFIG_FUSE_FS` and `CONFIG_VIRTIO_FS`. To share a
directory with virtio-fs, start `virtiofsd` on the host. For a directory owned
by your user, this unprivileged example maps guest root to your host UID/GID:

```sh
virtiofsd --socket-path=/tmp/uml-fs.sock --shared-dir="$PWD/share" \
  --tag=uml-share --sandbox=none --inode-file-handles=never --xattr \
  --translate-uid="map:0:$(id -u):1" --translate-gid="map:0:$(id -g):1"
```

Add `virtio_uml.device=/tmp/uml-fs.sock:26` to the UML command line, then run
`mount -t virtiofs uml-share /mnt` in the guest. The device ID is `26` and
`--tag` advertises the mount tag through vhost-user configuration. The daemon
accesses the directory as your host user; this example disables its additional
namespace/chroot sandbox. Add `--readonly` to enforce a read-only export.

The filesystem test compares virtio-fs with hostfs: reads/writes, shared mmap,
links, rename, permissions, guest-process locking, host updates, ownership,
extended attributes and read-only operation. Hostfs exposes host ownership
and lacks xattr support; virtiofsd can translate IDs and forward `user.*`
xattrs. Ordinary mmap works, but UML's transport provides no DAX window.

```ShellSession
$ nix-build -A user-mode-linux.tests.virtiofs -o result-uml-fs
$ cat result-uml-fs/comparison.txt
```

Nix's build sandbox blocks xattr syscalls, so the test records whether they
are available on the host. To exercise xattrs as well, run it outside the Nix
build sandbox on a filesystem supporting them:

```ShellSession
$ nix-build -A user-mode-linux.tests.virtiofs.runner -o result-uml-fs-runner
$ ./result-uml-fs-runner/bin/uml-virtiofs-test /tmp/uml-fs-results
$ cat /tmp/uml-fs-results/comparison.txt
```

### Architecture and modules {#sec-user-mode-linux-modules}

The compiler's host platform describes the instruction set and ABI. Kbuild's
`ARCH`, selected by `kernelArch`, describes the kernel port. Native x86-64 Linux
normally uses `kernelArch = "x86_64"`; UML uses `kernelArch = "um"` with
`SUBARCH=x86_64` or `SUBARCH=i386`, while keeping the corresponding host compiler.
The architecture is passed through configuration, kernel compilation, and
`kernel.commonMakeFlags`. `linuxPackagesFor` derives `kernelModuleMakeFlags`
from those same flags.

UML has the usual three kernel outputs:

| Output | Contents |
| --- | --- |
| `user-mode-linux` | `bin/linux` and `System.map` |
| `user-mode-linux.modules` | Modules under `lib/modules/<version>-uml` |
| `user-mode-linux.dev` | Configured source/build trees and `vmlinux` for development |

Copy the module output's `lib/modules` into the guest filesystem to use these
modules in your own image. Host kernel modules cannot be loaded into UML, even
when both kernels have the same upstream version and CPU architecture. Build
external modules against `pkgs.linuxPackagesFor pkgs.user-mode-linux`, using its
`kernelModuleMakeFlags` and the kernel's `moduleBuildDependencies`. The package's
boot test builds external C and Rust modules against the installed `dev`
output, then loads and unloads them inside the guest. The Rust test also
exercises allocation through the kernel's Rust API.

Rust support (`CONFIG_RUST=y`) is enabled on both x86-64 and i686 UML. External
Rust modules use Kbuild, just like C modules: for a source file named
`my_module.rs`, add `obj-m := my_module.o` to its Makefile. Use the Rust compiler
from `kernel.moduleBuildDependencies` so that it matches the compiler used for
the kernel and its Rust crate metadata. The kernel's `dev` output supplies
those crates and procedural macros. Build the module with the flags described
above, then load the resulting `my_module.ko` inside the guest. The
[upstream Rust documentation](https://docs.kernel.org/rust/index.html) describes
the kernel Rust APIs and development workflow.

### NixOS service tests {#sec-user-mode-linux-nixos-tests}

The NixOS testing framework accepts UML guests in its `umls` attribute set on
x86-64 Linux. These boot a full NixOS system with systemd, using the same Python
service-test methods as QEMU and nspawn machines. No KVM device or root access
is required. The guest root is a fresh tmpfs; `/nix/store` is shared read-only
through hostfs, and `/tmp/shared` provides the driver's writable file exchange.
No root disk image is required, and guest state is lost at shutdown.

The included tests reuse the existing lighttpd test script and the complete
five-machine Grafana basic test (plugins, SQLite, Unix-socket proxying,
PostgreSQL and MariaDB):

```ShellSession
$ nix-build -A nixosTests.user-mode-linux.lighttpd
$ nix-build -A nixosTests.user-mode-linux.grafana
```

This initial backend supports loopback networking. It rejects test VLANs and
does not provide QEMU monitor commands, emulated keyboards, graphical tests,
or an SSH backdoor. Use QEMU or nspawn for tests requiring those facilities.
The standalone kernel's networking and virtio transports can be configured
independently of this test runner.

## Maintainer information {#sec-linux-kernel-maintainer-information}

### Updating kernels {#sec-linux-updates}

Updating all kernels can be done with the following script:

```ShellSession
$ pkgs/os-specific/linux/kernel/update.sh
```

The change gets submitted like this:

* File a PR against `staging-nixos`.
  * Add a `backport staging-nixos-XX.XX` label for an automated backport.
    By using an additional PR, we get the automatic backport against stable without manual cherry-picks.
* Merge into `staging-nixos` or `staging-nixos-XX.XX`.
* File as PR from `staging-nixos` against `master` or `staging-nixos-XX.XX` against `release-xx.xx`.
* When all status checks are green, merge.

### Add a new (major) version of the Linux kernel {#sec-linux-add-new-kernel-version}

* When running `./pkgs/os-specific/linux/kernel/update.sh`, new kernel majors get discovered automatically.
* Prepare all Nix expressions for the new kernel
  * Instantiate the new kernel in `pkgs/top-level/linux-kernels.nix` in the `kernels`-section.
    ```nix
    {
      linux_X_Y = callPackage ../os-specific/linux/kernel/mainline.nix {
        branch = "X.Y";
        kernelPatches = [
          # any new patches required (it makes to look which patches are used by its predecessor)
        ];
      };
    }
    ```
  * Instantiate the package-set in `vanillaPackages`:
    ```nix
    {
      linux_X_Y = recurseIntoAttrs (packagesFor kernels.linux_X_Y);
    }
    ```
  * Update `linux_latest` to the new attribute.
* __SQUASH__ the changes into the `linux: init at …` commit.

### Policy for accepting new kernel flavours {#sec-linux-new-kernels}

No new downstream kernels are accepted into nixpkgs. That includes kernels that use the mainline
sourcetree, but a different configuration. Kernels for extended hardware support should go
to [nixos-hardware](github.com/NixOS/nixos-hardware) instead.
