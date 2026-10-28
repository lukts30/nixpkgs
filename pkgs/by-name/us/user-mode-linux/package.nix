{
  lib,
  stdenv,
  buildLinux,
  callPackage,
  linux,
  ...
}@args:

(buildLinux {
  pname = "user-mode-linux";
  inherit (linux) src version isLTS;
  kernelArch = "um";
  target = "linux";
  buildDTBs = false;
  kernelPatches = args.kernelPatches or [ ];
  randstructSeed = args.randstructSeed or "";

  # UML has its own devices and defaults, independent of the host's ISA.
  enableCommonConfig = false;
  autoModules = false;
  features = {
    efiBootStub = false;
    ia32Emulation = false;
    netfilterRPFilter = false;
    rust = true;
  }
  // (args.features or { });

  # Unlike a native kernel, UML links against the host C library.
  extraMakeFlags = [
    "CC=${lib.getExe' stdenv.cc "${stdenv.cc.targetPrefix}cc"}"
    "SUBARCH=${stdenv.hostPlatform.linuxArch}"
    # The upstream bindgen target table only handles SUBARCH=x86, not the
    # explicit x86_64/i386 values used to select the correct UML defconfig.
    "BINDGEN_TARGET=${stdenv.hostPlatform.config}"
  ];

  modDirVersion = "${linux.modDirVersion}-uml";
  structuredExtraConfig = with lib.kernel; {
    LOCALVERSION = freeform "-uml";
    LOCALVERSION_AUTO = no;
    EXPERT = yes;
    LD_SCRIPT_DYN_RPATH = no;
    MODULES = yes;
    MODULE_UNLOAD = yes;
    MODULE_SIG = yes;
    MODULE_SIG_ALL = yes;
    MODULE_SIG_KEY = freeform "";
    RUST = yes;
    BLK_DEV_INITRD = yes;
    DEVTMPFS = yes;
    DEVTMPFS_MOUNT = yes;
    HOSTFS = yes;
    EXT4_FS = yes;
    FW_LOADER = yes;
    UML_NET_VECTOR = yes;
    IPV6 = yes;

    # UML exposes virtio devices directly over vhost-user Unix sockets.
    VIRTIO_UML = yes;
    VIRTIO_BLK = yes;
    VIRTIO_NET = yes;
    VSOCKETS = yes;
    VIRTIO_VSOCKETS = yes;
    FUSE_FS = yes;
    VIRTIO_FS = yes;
    HW_RANDOM_VIRTIO = yes;
    VIRTIO_CONSOLE = yes;
    SCSI = yes;
    BLK_DEV_SD = yes;
    SCSI_VIRTIO = yes;

    # Run systemd services with their usual namespace and cgroup isolation.
    PID_NS = yes;
    USER_NS = yes;
    MEMCG = yes;
    CGROUP_PIDS = yes;
    BPF_SYSCALL = yes;
    CGROUP_BPF = yes;
    TMPFS_POSIX_ACL = yes;
    EXT4_FS_POSIX_ACL = yes;
    EXT4_FS_SECURITY = yes;
    SECURITY = yes;
    SECURITYFS = yes;
    FANOTIFY = yes;
    AUTOFS_FS = yes;
    CRYPTO_USER_API_HASH = yes;
    CRYPTO_HMAC = yes;
    CRYPTO_SHA256 = yes;
  };

  extraMeta = {
    description = "Linux kernel running as an unprivileged userspace process";
    homepage = "https://docs.kernel.org/virt/uml/user_mode_linux_howto_v2.html";
    mainProgram = "linux";
    platforms = [
      "x86_64-linux"
      "i686-linux"
    ];
  };
}).overrideAttrs
  (
    final: old: {
      patches =
        old.patches
        ++ [ ./rust-uml-code-model.patch ]
        ++ lib.optional (lib.versionOlder linux.version "6.19") ./rust-debugfs-32bit.patch;

      # glibc loads libgcc_s for pthread_cancel during UML shutdown.
      # Only link it into the dynamic kernel executable, not the freestanding
      # syscall stub which is also linked using CC.
      postPatch = old.postPatch + ''
        substituteInPlace arch/um/Makefile \
          --replace-fail 'LINK-$(CONFIG_LD_SCRIPT_DYN) += -no-pie' \
                         'LINK-$(CONFIG_LD_SCRIPT_DYN) += -no-pie -lgcc_s'
      '';

      # arch/um has no install target; retain the shared module installation
      # and development-tree preparation, and install the executable ourselves.
      installTargets = [ "modules_install" ];
      preInstall = ''
        install -Dm755 "$buildRoot/linux" "$out/bin/linux"
        cp "$buildRoot/System.map" "$out/"

        # The shared builder prunes sources to prepare `dev`. UML includes
        # these additional makefiles even when building external modules.
        chmod u-w arch/um/Makefile* arch/x86/Makefile.um
      ''
      + lib.optionalString stdenv.hostPlatform.isx86_32 ''
        chmod u-w arch/x86/Makefile_32.cpu
      '';
      # Strip the installed executable's references to the source in `dev`.
      # The unstripped vmlinux remains available in the development output.
      stripDebugList = old.stripDebugList ++ [ "bin" ];

      passthru = old.passthru // {
        benchmarks.filesystems = callPackage ./filesystem-benchmark.nix { kernel = final.finalPackage; };
        benchmarks.block = callPackage ./block-benchmark.nix { kernel = final.finalPackage; };
        tests = old.passthru.tests // {
          inherit (callPackage ./tests.nix { kernel = final.finalPackage; }) boot virtio;
          virtiofs = callPackage ./virtiofs-test.nix { kernel = final.finalPackage; };
          devices = callPackage ./device-tests.nix { kernel = final.finalPackage; };
          qemuNetwork = callPackage ./qemu-bridge-test.nix { kernel = final.finalPackage; };
          inherit (callPackage ./block-root-test.nix { kernel = final.finalPackage; })
            blockRoot
            qemuBlockRoot
            ;
          inherit (callPackage ./backend-tests.nix { kernel = final.finalPackage; }) slirp passt vsock;
        };
        inherit (callPackage ./tests.nix { kernel = final.finalPackage; }) demo;
      };
    }
  )
