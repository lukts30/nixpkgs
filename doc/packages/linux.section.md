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
boot test builds a small external module against the installed `dev` output,
then loads and unloads it inside the guest.

This package provides a standalone UML kernel and a minimal demonstration
guest. It does not provide a complete NixOS UML system configuration.

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
