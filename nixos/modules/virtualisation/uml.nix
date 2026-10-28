# A diskless NixOS guest running on an unprivileged User Mode Linux kernel.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.virtualisation.uml;
  interfaces = lib.attrValues config.virtualisation.allInterfaces;
  interface = lib.head interfaces;
  address = config.networking.primaryIPAddress;
  gateway = "192.168.${toString interface.vlan}.254";
  forwardPorts =
    protocol:
    let
      ports =
        config.networking.firewall."allowed${protocol}Ports" ++ cfg.networking."extra${protocol}Ports";
      ranges = config.networking.firewall."allowed${protocol}PortRanges";
      specifications =
        map toString (lib.unique ports)
        ++ map (range: "${toString range.from}-${toString range.to}") ranges;
    in
    if specifications == [ ] then "none" else "${address}/${lib.concatStringsSep "," specifications}";
in
{
  imports = [ ./guest-networking-options.nix ];

  options.virtualisation = {
    installBootLoader = lib.mkOption {
      type = lib.types.bool;
      default = false;
      internal = true;
    };
    uml.networking = {
      enable = lib.mkEnableOption ''
        TCP/UDP networking through passt in a private user/network namespace.
        Supports one automatically addressed IPv4 interface per guest on a
        single test VLAN. Forwards the firewall's allowed ports and port ranges
      '';
      extraTCPPorts = lib.mkOption {
        type = lib.types.listOf lib.types.port;
        default = [ ];
        description = "Additional TCP ports to forward from other UML guests.";
      };
      extraUDPPorts = lib.mkOption {
        type = lib.types.listOf lib.types.port;
        default = [ ];
        description = "Additional UDP ports to forward from other UML guests.";
      };
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
        assertion =
          if cfg.networking.enable then
            lib.length interfaces == 1
            && interface.assignIP
            && interface.vlan < 256
            && config.virtualisation.test.nodeNumber < 254
            && !config.networking.enableIPv6
          else
            interfaces == [ ];
        message = "UML passt requires one automatically addressed IPv4 interface (VLAN 0..255, node 1..253); otherwise UML supports loopback only.";
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
    networking.enableIPv6 = lib.mkIf cfg.networking.enable (lib.mkDefault false);
    networking.primaryIPv6Address = lib.mkIf cfg.networking.enable (lib.mkForce "");
    networking.interfaces = lib.mkIf cfg.networking.enable {
      ${interface.name}.ipv6.addresses = lib.mkForce [ ];
    };
    # Passt learns the generated guest MAC from outbound traffic. Announce it
    # before accepting connections, including on otherwise silent servers.
    systemd.services.uml-network-ready = lib.mkIf cfg.networking.enable {
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      before = [ "backdoor.service" ];
      serviceConfig.Type = "oneshot";
      serviceConfig.RemainAfterExit = true;
      script = ''
        ${pkgs.iputils}/bin/arping -U -c 1 -w 1 -I ${lib.escapeShellArg interface.name} ${lib.escapeShellArg address}
      '';
    };
    # Passt supplies a virtio-net device with a generated MAC address. Match the
    # sole virtio device rather than relying on that address for test NIC naming.
    systemd.network.links."10-uml" = lib.mkIf cfg.networking.enable {
      matchConfig.Driver = "virtio_net";
      linkConfig.Name = interface.name;
    };
    virtualisation.vlans = lib.mkDefault (lib.optional cfg.networking.enable 1);
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

    system.build.umlPasst = lib.mkIf cfg.networking.enable (
      pkgs.writeShellScriptBin "run-${config.networking.hostName}-passt" ''
        exec ${lib.getExe pkgs.passt} --foreground --one-off --vhost-user \
          --socket="$1" --runas=0:0 --ipv4-only --interface=lo \
          --address=${address} --netmask=24 --gateway=${gateway} \
          --map-host-loopback=${gateway} --outbound=${address} \
          --dns=none --search=none \
          ${lib.escapeShellArgs [
            "--tcp-ports=${forwardPorts "TCP"}"
            "--udp-ports=${forwardPorts "UDP"}"
          ]}
      ''
    );
  };
}
