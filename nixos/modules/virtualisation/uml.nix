# A diskless NixOS guest running on an unprivileged User Mode Linux kernel.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.virtualisation.uml;
in
{
  imports = [ ./guest-networking-options.nix ];

  options.virtualisation = {
    installBootLoader = lib.mkOption {
      type = lib.types.bool;
      default = false;
      internal = true;
    };
    uml.memorySize = lib.mkOption {
      type = lib.types.ints.positive;
      default = 1024;
      description = "Memory available to the UML guest, in MiB.";
    };
  };

  config = {
    assertions = [
      {
        assertion = pkgs.stdenv.hostPlatform.system == "x86_64-linux";
        message = "The NixOS UML runner currently supports x86_64-linux hosts.";
      }
      {
        assertion = config.virtualisation.allInterfaces == { };
        message = "The UML test backend currently supports loopback networking only.";
      }
    ];
    boot.kernelPackages = lib.mkDefault (pkgs.linuxPackagesFor pkgs.user-mode-linux);
    boot.initrd.includeDefaultModules = false;
    boot.initrd.kernelModules = [ ];
    boot.initrd.availableKernelModules.efivarfs = lib.mkForce false;
    boot.initrd.systemd.tpm2.enable = false;
    systemd.tpm2.enable = false;
    boot.loader.grub.enable = false;
    system.boot.loader.kernelFile = "bin/linux";
    boot.kernelParams = [ "console=tty0" ];
    # Unlike hardware serial consoles, UML's tty0 is not tagged by systemd's
    # default tty rule, but the test backdoor waits for its device unit.
    services.udev.extraRules = ''
      SUBSYSTEM=="tty", KERNEL=="tty0", TAG+="systemd"
    '';
    boot.initrd.services.udev.rules = ''
      SUBSYSTEM=="tty", KERNEL=="tty0", TAG+="systemd"
    '';
    networking.useDHCP = lib.mkDefault false;
    networking.firewall.enable = lib.mkDefault false;
    virtualisation.vlans = lib.mkDefault [ ];
    documentation.info.enable = lib.mkDefault false;
    # UML uses fixed ASLR entropy and has no ARCH_MMAP_RND_BITS_MAX knob.
    environment.etc."sysctl.d/55-nixos-aslr-entropy.conf".enable = false;

    fileSystems."/" = {
      device = "none";
      fsType = "tmpfs";
      options = [ "mode=755" ];
    };
    fileSystems."/nix/store" = {
      device = "none";
      fsType = "hostfs";
      options = [
        "hostfs=/nix/store"
        "ro"
      ];
      neededForBoot = true;
    };

    # The test driver supplies this path at runtime, independently of the
    # machine name. Mounting it here also enables BaseMachine.copy_* methods.
    systemd.services.uml-shared-directory = {
      wantedBy = [ "multi-user.target" ];
      before = [ "backdoor.service" ];
      path = [ pkgs.util-linux ];
      script = ''
        for arg in $(cat /proc/cmdline); do
          case "$arg" in
            uml_shared=*)
              mkdir -p /tmp/shared
              mount -t hostfs -o "hostfs=''${arg#uml_shared=}" none /tmp/shared
              ;;
          esac
        done
      '';
      serviceConfig.Type = "oneshot";
      serviceConfig.RemainAfterExit = true;
    };

    system.build.uml = pkgs.writeShellScriptBin "run-${config.networking.hostName}-uml" ''
      set -eu
      : "''${TMPDIR:=$(mktemp -d)}"
      export TMPDIR
      exec ${lib.getExe config.boot.kernelPackages.kernel} \
        mem=${toString cfg.memorySize}M \
        initrd=${config.system.build.initialRamdisk}/initrd \
        init=${config.system.build.toplevel}/init \
        ${lib.escapeShellArgs config.boot.kernelParams} \
        con=null con0=fd:0,fd:1 ssl=null uml_dir="$TMPDIR" \
        "uml_shared=''${UML_SHARED_DIR:-$TMPDIR}" "$@"
    '';
  };
}
